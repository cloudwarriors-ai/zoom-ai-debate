#!/usr/bin/env python3
"""
Zoom SDK Worker Process

Runs as a standalone subprocess. Communicates with the parent process
via stdin (JSON commands) and stdout (JSON responses prefixed with "JSON:").

The Zoom Meeting SDK only works on Linux (Ubuntu 22.04). When running on
macOS for development, the worker operates in stub mode — it simulates
SDK behavior so the rest of the pipeline can be tested end-to-end.
"""

import asyncio
import base64
import json
import logging
import os
import select
import signal
import sys
import threading
import time
from collections import deque

logger = logging.getLogger("zoom_worker")

# ---------------------------------------------------------------------------
# Try to import the real Zoom SDK. If unavailable, fall back to stub mode.
# ---------------------------------------------------------------------------

SDK_AVAILABLE = False

try:
    import jwt as pyjwt  # noqa: F401 — used in authenticate()

    # Qt must be imported before zoom_meeting_sdk
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("DISPLAY", ":99")

    from PyQt5.QtCore import QTimer  # noqa: E402
    from PyQt5.QtWidgets import QApplication  # noqa: E402

    import zoom_meeting_sdk as zoom  # noqa: E402

    SDK_AVAILABLE = True
except ImportError:
    zoom = None  # type: ignore[assignment]
    QApplication = None  # type: ignore[assignment,misc]
    QTimer = None  # type: ignore[assignment,misc]
    pyjwt = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Protocol helpers
# ---------------------------------------------------------------------------

def send_response(response: dict) -> None:
    """Write a JSON response to stdout using the 'JSON:' prefix protocol."""
    json_str = json.dumps(response)
    print("JSON:" + json_str, flush=True)


# ---------------------------------------------------------------------------
# AudioSource — queues PCM16 audio and feeds it to the SDK virtual mic
# ---------------------------------------------------------------------------

class AudioSource:
    """Virtual microphone that sends queued PCM16 audio to the Zoom SDK."""

    def __init__(self, sample_rate: int = 32000):
        self.sample_rate = sample_rate
        self.chunk_samples = sample_rate // 50  # 20 ms chunks
        self.chunk_bytes = self.chunk_samples * 2  # 16-bit = 2 bytes per sample

        self._buffer = bytearray()
        self._lock = threading.Lock()

        self.sender = None  # Set by SDK callback
        self.is_sending = False
        self._send_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._on_ready_callback = None  # Called after mic init to trigger unmute

    # -- SDK callbacks (real mode) ------------------------------------------

    def on_mic_initialize(self, sender) -> None:
        self.sender = sender
        send_response({"action": "mic_init"})
        # Start send loop immediately — virtual mic bypasses mute state
        if not self.is_sending:
            self.is_sending = True
            self._stop.clear()
            self._send_thread = threading.Thread(target=self._send_loop, daemon=True)
            self._send_thread.start()
            send_response({"action": "mic_start", "source": "on_init"})
        # Also try unmute if callback is set
        if self._on_ready_callback:
            self._on_ready_callback()

    def on_mic_start_send(self) -> None:
        if not self.is_sending:
            self.is_sending = True
            self._stop.clear()
            self._send_thread = threading.Thread(target=self._send_loop, daemon=True)
            self._send_thread.start()
        send_response({"action": "mic_start", "source": "on_start_send"})

    def on_mic_stop_send(self) -> None:
        self.is_sending = False
        self._stop.set()
        send_response({"action": "mic_stop"})

    def on_mic_uninitialized(self) -> None:
        self.sender = None
        self._stop.set()

    # -- Public API ---------------------------------------------------------

    def queue_audio(self, data: bytes) -> None:
        with self._lock:
            self._buffer.extend(data)

    def clear_buffer(self) -> int:
        with self._lock:
            n = len(self._buffer)
            self._buffer.clear()
            return n

    # -- Send loop ----------------------------------------------------------

    def _send_loop(self) -> None:
        interval = self.chunk_samples / self.sample_rate  # 0.02 s
        silence = bytes(self.chunk_bytes)
        silence_count = 0

        while not self._stop.is_set() and self.sender:
            t0 = time.time()
            chunk: bytes | None = None

            with self._lock:
                if len(self._buffer) >= self.chunk_bytes:
                    chunk = bytes(self._buffer[: self.chunk_bytes])
                    del self._buffer[: self.chunk_bytes]

            if chunk:
                self.sender.send(chunk, self.sample_rate, zoom.ZoomSDKAudioChannel_Mono)
                silence_count = 0
            elif silence_count < 50:
                self.sender.send(silence, self.sample_rate, zoom.ZoomSDKAudioChannel_Mono)
                silence_count += 1

            elapsed = time.time() - t0
            remaining = interval - elapsed
            if remaining > 0.001:
                time.sleep(remaining)


# ---------------------------------------------------------------------------
# StubAudioSource — used when the real SDK is not available
# ---------------------------------------------------------------------------

class StubAudioSource:
    """No-op audio source that logs chunks for mock mode."""

    def __init__(self, sample_rate: int = 32000):
        self.sample_rate = sample_rate
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._total_bytes = 0

    def queue_audio(self, data: bytes) -> None:
        with self._lock:
            self._buffer.extend(data)
            self._total_bytes += len(data)

    def clear_buffer(self) -> int:
        with self._lock:
            n = len(self._buffer)
            self._buffer.clear()
            return n


# ---------------------------------------------------------------------------
# ZoomWorker — wraps the Zoom SDK (real mode)
# ---------------------------------------------------------------------------

class ZoomWorker:
    """Manages Zoom SDK lifecycle: init, auth, join, audio, leave."""

    def __init__(self, sample_rate: int = 32000):
        self.auth_service = None
        self.meeting_service = None
        self.is_authenticated = False
        self.is_in_meeting = False

        # Prevent GC of callback pointers
        self._auth_cb = None
        self._meeting_cb = None
        self._mic_cb = None
        self._audio_helper = None

        self.audio_source = AudioSource(sample_rate)

    # -- SDK init / auth ----------------------------------------------------

    def init_sdk(self) -> tuple[bool, str | None]:
        try:
            init_param = zoom.InitParam()
            init_param.strWebDomain = "https://zoom.us"
            init_param.enableLogByDefault = True

            result = zoom.InitSDK(init_param)
            if result != zoom.SDKERR_SUCCESS:
                return False, f"InitSDK failed: {result}"

            self.auth_service = zoom.CreateAuthService()
            self.meeting_service = zoom.CreateMeetingService()

            if not self.auth_service or not self.meeting_service:
                return False, "Failed to create SDK services"

            self._auth_cb = zoom.AuthServiceEventCallbacks(
                onAuthenticationReturnCallback=self._on_auth
            )
            self.auth_service.SetEvent(self._auth_cb)

            self._meeting_cb = zoom.MeetingServiceEventCallbacks(
                onMeetingStatusChangedCallback=self._on_meeting_status
            )
            self.meeting_service.SetEvent(self._meeting_cb)

            return True, None
        except Exception as exc:
            return False, str(exc)

    def authenticate(self, client_id: str, client_secret: str) -> tuple[bool, str | None]:
        try:
            iat = int(time.time())
            payload = {"appKey": client_id, "iat": iat, "exp": iat + 86400, "tokenExp": iat + 86400}
            token = pyjwt.encode(payload, client_secret, algorithm="HS256")

            ctx = zoom.AuthContext()
            ctx.jwt_token = token

            result = self.auth_service.SDKAuth(ctx)
            if result != zoom.SDKERR_SUCCESS:
                return False, f"SDKAuth failed: {result}"
            return True, None
        except Exception as exc:
            return False, str(exc)

    # -- SDK callbacks ------------------------------------------------------

    def _on_auth(self, result) -> None:
        if result == zoom.AUTHRET_SUCCESS:
            self.is_authenticated = True
            send_response({"action": "auth", "success": True})
            send_response({"action": "ready", "sdk_ready": True})
        else:
            self.is_authenticated = False
            send_response({"action": "auth", "success": False, "error": str(result)})
            send_response({"action": "ready", "sdk_ready": False})

    def _on_meeting_status(self, status, result) -> None:
        STATUS_MAP = {
            zoom.MEETING_STATUS_IDLE: "IDLE",
            zoom.MEETING_STATUS_CONNECTING: "CONNECTING",
            zoom.MEETING_STATUS_WAITINGFORHOST: "WAITING_FOR_HOST",
            zoom.MEETING_STATUS_INMEETING: "IN_MEETING",
            zoom.MEETING_STATUS_DISCONNECTING: "DISCONNECTING",
            zoom.MEETING_STATUS_RECONNECTING: "RECONNECTING",
            zoom.MEETING_STATUS_ENDED: "ENDED",
            zoom.MEETING_STATUS_FAILED: "FAILED",
            zoom.MEETING_STATUS_IN_WAITING_ROOM: "IN_WAITING_ROOM",
        }
        status_str = STATUS_MAP.get(status, str(status))
        send_response({"action": "meeting_status", "status": status_str, "result": str(result)})

        if status == zoom.MEETING_STATUS_INMEETING:
            self.is_in_meeting = True
            self._setup_audio()
        elif status in (zoom.MEETING_STATUS_ENDED, zoom.MEETING_STATUS_FAILED, zoom.MEETING_STATUS_IDLE):
            self.is_in_meeting = False

    # -- Meeting join / leave -----------------------------------------------

    def join_meeting(self, meeting_id: str, password: str, display_name: str) -> None:
        if not self.is_authenticated:
            send_response({"action": "join", "success": False, "error": "Not authenticated"})
            return
        try:
            clean_id = meeting_id.replace(" ", "").replace("-", "")

            wl = zoom.JoinParam4WithoutLogin()
            wl.meetingNumber = int(clean_id)
            wl.userName = display_name
            if password:
                wl.psw = password
            wl.isVideoOff = True
            wl.isAudioOff = False

            jp = zoom.JoinParam()
            jp.userType = zoom.SDKUserType.SDK_UT_WITHOUT_LOGIN
            jp.param = wl

            result = self.meeting_service.Join(jp)
            success = result == zoom.SDKERR_SUCCESS
            send_response({"action": "join", "success": success, "result": str(result)})
        except Exception as exc:
            send_response({"action": "join", "success": False, "error": str(exc)})

    def leave_meeting(self) -> None:
        try:
            self.audio_source._stop.set()
            self.audio_source.is_sending = False

            if self.meeting_service and self.is_in_meeting:
                result = self.meeting_service.Leave(zoom.LEAVE_MEETING)
                self.is_in_meeting = False
                send_response({"action": "leave", "success": result == zoom.SDKERR_SUCCESS})
            else:
                send_response({"action": "leave", "success": True, "message": "Not in meeting"})
        except Exception as exc:
            send_response({"action": "leave", "success": False, "error": str(exc)})

    # -- Audio setup --------------------------------------------------------

    def _setup_audio(self) -> None:
        try:
            self._audio_helper = zoom.GetAudioRawdataHelper()
            if not self._audio_helper:
                send_response({"action": "error", "message": "No audio helper"})
                return

            # Set up unmute callback — called from on_mic_initialize after sender is ready
            def _unmute_after_init():
                audio_ctrl = self.meeting_service.GetMeetingAudioController()
                participants_ctrl = self.meeting_service.GetMeetingParticipantsController()
                if audio_ctrl and participants_ctrl:
                    myself = participants_ctrl.GetMySelfUser()
                    my_user_id = myself.GetUserID() if myself else 0
                    can_unmute = audio_ctrl.CanUnMuteBySelf()
                    send_response({"action": "debug", "message": f"user_id={my_user_id}, can_unmute_self={can_unmute}"})
                    result = audio_ctrl.UnMuteAudio(my_user_id)
                    send_response({"action": "unmute_after_mic_init", "result": str(result), "user_id": my_user_id, "can_unmute": can_unmute})

            self.audio_source._on_ready_callback = _unmute_after_init

            self._mic_cb = zoom.ZoomSDKVirtualAudioMicEventCallbacks(
                onMicInitializeCallback=self.audio_source.on_mic_initialize,
                onMicStartSendCallback=self.audio_source.on_mic_start_send,
                onMicStopSendCallback=self.audio_source.on_mic_stop_send,
                onMicUninitializedCallback=self.audio_source.on_mic_uninitialized,
            )

            # Register virtual mic with SDK
            result = self._audio_helper.setExternalAudioSource(self._mic_cb)
            send_response({"action": "set_audio_source", "result": str(result)})

            # Join VoIP channel — required for unmute permission
            audio_ctrl = self.meeting_service.GetMeetingAudioController()
            if audio_ctrl:
                voip_result = audio_ctrl.JoinVoip()
                send_response({"action": "join_voip", "result": str(voip_result)})

            # on_mic_initialize fires async → calls UnMuteAudio → triggers on_mic_start_send
            send_response({"action": "audio_setup", "success": True})
        except Exception as exc:
            send_response({"action": "error", "message": f"Audio setup failed: {exc}"})

    # -- Mute / unmute ------------------------------------------------------

    def mute_audio(self) -> None:
        try:
            if self.meeting_service:
                ctrl = self.meeting_service.GetMeetingAudioController()
                if ctrl:
                    ctrl.MuteAudio(0)
                    send_response({"action": "mute_audio", "success": True})
                    return
            send_response({"action": "mute_audio", "success": False, "error": "No controller"})
        except Exception as exc:
            send_response({"action": "mute_audio", "success": False, "error": str(exc)})

    def unmute_audio(self) -> None:
        try:
            if self.meeting_service:
                audio_ctrl = self.meeting_service.GetMeetingAudioController()
                participants_ctrl = self.meeting_service.GetMeetingParticipantsController()
                if audio_ctrl and participants_ctrl:
                    myself = participants_ctrl.GetMySelfUser()
                    my_user_id = myself.GetUserID() if myself else 0
                    can_unmute = audio_ctrl.CanUnMuteBySelf()
                    result = audio_ctrl.UnMuteAudio(my_user_id)
                    send_response({"action": "unmute_audio", "result": str(result), "user_id": my_user_id, "can_unmute": can_unmute})
                    return
            send_response({"action": "unmute_audio", "success": False, "error": "No controller"})
        except Exception as exc:
            send_response({"action": "unmute_audio", "success": False, "error": str(exc)})

    # -- Cleanup ------------------------------------------------------------

    def cleanup(self) -> None:
        self.audio_source._stop.set()
        self.audio_source.sender = None

        if self.meeting_service and self.is_in_meeting:
            try:
                self.meeting_service.Leave(zoom.LEAVE_MEETING)
            except Exception:
                pass
            self.is_in_meeting = False

        try:
            if self.meeting_service:
                zoom.DestroyMeetingService(self.meeting_service)
            if self.auth_service:
                zoom.DestroyAuthService(self.auth_service)
            zoom.CleanUPSDK()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# StubWorker — simulates SDK for macOS development
# ---------------------------------------------------------------------------

class StubWorker:
    """Simulates Zoom SDK behaviour for local development."""

    def __init__(self, sample_rate: int = 32000):
        self.is_authenticated = False
        self.is_in_meeting = False
        self.audio_source = StubAudioSource(sample_rate)

    def join_meeting(self, meeting_id: str, password: str, display_name: str) -> None:
        send_response({"action": "join", "success": True, "stub": True})
        # Simulate async connection sequence
        threading.Thread(target=self._simulate_join, daemon=True).start()

    def _simulate_join(self) -> None:
        time.sleep(0.3)
        send_response({"action": "meeting_status", "status": "CONNECTING", "result": "0"})
        time.sleep(0.5)
        send_response({"action": "meeting_status", "status": "IN_MEETING", "result": "0"})
        self.is_in_meeting = True

    def leave_meeting(self) -> None:
        self.is_in_meeting = False
        send_response({"action": "meeting_status", "status": "ENDED", "result": "0"})
        send_response({"action": "leave", "success": True, "stub": True})

    def mute_audio(self) -> None:
        send_response({"action": "mute_audio", "success": True, "stub": True})

    def unmute_audio(self) -> None:
        send_response({"action": "unmute_audio", "success": True, "stub": True})

    def cleanup(self) -> None:
        self.is_in_meeting = False


# ---------------------------------------------------------------------------
# Command dispatcher
# ---------------------------------------------------------------------------

def handle_command(worker: ZoomWorker | StubWorker, cmd: dict) -> bool:
    """Process a single JSON command. Returns False if the worker should exit."""
    action = cmd.get("action")

    if action == "join":
        worker.join_meeting(
            cmd.get("meeting_id", ""),
            cmd.get("password", ""),
            cmd.get("name", "AI Debater"),
        )

    elif action == "audio_chunk":
        data = base64.b64decode(cmd.get("data", ""))
        worker.audio_source.queue_audio(data)
        send_response({"action": "audio_received", "bytes": len(data)})

    elif action == "mute_audio":
        worker.mute_audio()

    elif action == "unmute_audio":
        worker.unmute_audio()

    elif action == "leave":
        worker.leave_meeting()

    elif action == "quit":
        worker.cleanup()
        return False

    elif action == "status":
        send_response({
            "action": "status",
            "authenticated": getattr(worker, "is_authenticated", True),
            "in_meeting": worker.is_in_meeting,
        })

    elif action == "clear_audio":
        n = worker.audio_source.clear_buffer()
        send_response({"action": "clear_audio", "success": True, "cleared_bytes": n})

    else:
        send_response({"action": "error", "message": f"Unknown action: {action}"})

    return True


# ---------------------------------------------------------------------------
# Main: SDK mode (Qt event loop + stdin polling)
# ---------------------------------------------------------------------------

def main_sdk() -> int:
    """Entry point when the real Zoom SDK is available."""
    app = QApplication(sys.argv)

    worker = ZoomWorker(sample_rate=int(os.environ.get("ZOOM_SAMPLE_RATE", "32000")))

    ok, err = worker.init_sdk()
    if not ok:
        send_response({"action": "init", "success": False, "error": err})
        send_response({"action": "ready", "sdk_ready": False})
        return 1

    send_response({"action": "init", "success": True})

    client_id = os.environ.get("ZOOM_CLIENT_ID", "")
    client_secret = os.environ.get("ZOOM_CLIENT_SECRET", "")
    ok, err = worker.authenticate(client_id, client_secret)
    if not ok:
        send_response({"action": "auth", "success": False, "error": err})
        send_response({"action": "ready", "sdk_ready": False})
        return 1

    # Poll for auth result
    check_count = [0]

    def poll_auth() -> None:
        check_count[0] += 1
        auth_result = worker.auth_service.GetAuthResult()
        if auth_result == zoom.AUTHRET_SUCCESS:
            worker.is_authenticated = True
            send_response({"action": "auth", "success": True})
            send_response({"action": "ready", "sdk_ready": True})
        elif auth_result != zoom.AUTHRET_NONE:
            send_response({"action": "auth", "success": False, "error": str(auth_result)})
            send_response({"action": "ready", "sdk_ready": False})
        elif check_count[0] < 100:
            QTimer.singleShot(100, poll_auth)
        else:
            send_response({"action": "ready", "sdk_ready": False})

    QTimer.singleShot(100, poll_auth)

    # Stdin polling via QTimer
    def poll_stdin() -> None:
        try:
            if select.select([sys.stdin], [], [], 0)[0]:
                line = sys.stdin.readline()
                if line:
                    line = line.strip()
                    if line:
                        try:
                            cmd = json.loads(line)
                            if not handle_command(worker, cmd):
                                app.quit()
                        except json.JSONDecodeError:
                            pass
        except Exception:
            pass

    stdin_timer = QTimer()
    stdin_timer.timeout.connect(poll_stdin)
    stdin_timer.start(10)

    def on_signal(sig, frame):
        worker.cleanup()
        app.quit()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    return app.exec_()


# ---------------------------------------------------------------------------
# Main: Stub mode (asyncio stdin loop)
# ---------------------------------------------------------------------------

def main_stub() -> int:
    """Entry point when the SDK is not available (macOS dev)."""
    send_response({"action": "init", "success": True, "stub": True})
    send_response({"action": "ready", "sdk_ready": True, "stub": True})

    worker = StubWorker(sample_rate=int(os.environ.get("ZOOM_SAMPLE_RATE", "32000")))
    worker.is_authenticated = True

    running = True

    def on_signal(sig, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    while running:
        try:
            if select.select([sys.stdin], [], [], 0.05)[0]:
                line = sys.stdin.readline()
                if not line:
                    break
                line = line.strip()
                if line:
                    try:
                        cmd = json.loads(line)
                        if not handle_command(worker, cmd):
                            break
                    except json.JSONDecodeError:
                        pass
        except Exception:
            pass

    return 0


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> int:
    if SDK_AVAILABLE:
        send_response({"action": "debug", "message": "SDK available, starting real mode"})
        return main_sdk()
    else:
        send_response({"action": "debug", "message": "SDK not available, starting stub mode"})
        return main_stub()


if __name__ == "__main__":
    sys.exit(main())
