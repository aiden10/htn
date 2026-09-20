"""Asynchronous transport boundary for Wi-Fi HTN badges.

The badge firmware owns its device token and its connection to the badge
service.  Shutterdex only needs a badge's public HTN ID plus the app key that
the player deliberately pairs with the Shutterdex service.  Consequently this
module deliberately has *no* ``device_token`` field, parameter, URL option,
or persistence code.

``HTNBadgeGateway`` is the application-facing API.  It has one outgoing queue
and a smooth per-badge command rate limiter for every registered badge.  It
also forwards incoming button/NFC/mode/etc. events to registered handlers.
The actual service wire format is kept behind ``BadgeTransport`` so the UI and
game code can be tested entirely in memory:

    transport = InMemoryBadgeTransport()
    gateway = HTNBadgeGateway(transport)
    await gateway.register_badge(BadgeCredentials("abcde", "app-key"))
    await gateway.send("abcde", BadgeCommand("draw_text", {"text": "Hi"}))

``RestBadgeTransport`` uses only the Python standard library.  The optional
``WebSocketBadgeTransport`` lazily imports the external ``websockets`` package
only when it is instantiated.  This keeps normal imports and tests working
without a new dependency. ``HTNServiceWebSocketTransport`` is the concrete
adapter for the public HTN OS app WebSocket protocol. Install WebSocket support
in the deployment environment with:

    py -m pip install websockets

Its URL, headers, and JSON encoding are configurable because the generic
transport can also be used with a local/self-hosted badge service. The concrete
HTN OS adapter keeps the official protocol details in one auditable place.

Run ``py htn_gateway.py --self-test`` for a dependency-free smoke test.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
import inspect
import json
import logging
from time import monotonic
from typing import Any, Protocol, TypeAlias
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from uuid import uuid4


LOGGER = logging.getLogger(__name__)

JsonObject: TypeAlias = dict[str, Any]
EventHandler: TypeAlias = Callable[["BadgeEvent"], Awaitable[None] | None]
CommandEncoder: TypeAlias = Callable[["BadgeCredentials", "BadgeCommand"], str | bytes]
EventDecoder: TypeAlias = Callable[[str, str | bytes], "BadgeEvent | None"]
ConnectPayloadFactory: TypeAlias = Callable[["BadgeCredentials"], Mapping[str, Any] | None]


class GatewayError(RuntimeError):
    """Base class for errors raised by this module."""


class BadgeNotRegisteredError(GatewayError):
    """Raised when a command is sent to a badge without a live gateway session."""


class BadgeAlreadyRegisteredError(GatewayError):
    """Raised when a different app key is supplied for an existing badge session."""


class BadgeQueueFullError(GatewayError):
    """Raised instead of silently losing a command when a badge is overloaded."""


class TransportError(GatewayError):
    """Raised by a transport when the service cannot accept a command."""


class OptionalWebsocketsDependencyError(GatewayError):
    """Raised when WebSocket support is requested without installing websockets."""


@dataclass(frozen=True, slots=True)
class BadgeCredentials:
    """The only secrets an application server needs for one badge.

    ``app_key`` is intentionally excluded from repr/logging.  Persisting it
    encrypted is the responsibility of the database layer; this gateway only
    retains it in memory while the badge session is registered.
    """

    badge_id: str
    app_key: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.badge_id or not self.badge_id.strip():
            raise ValueError("badge_id cannot be blank")
        if not self.app_key or not self.app_key.strip():
            raise ValueError("app_key cannot be blank")


@dataclass(frozen=True, slots=True)
class BadgeCommand:
    """One logical instruction headed to a badge.

    ``name`` should be a service-level operation such as ``draw_text`` or
    ``clear``.  The concrete transport maps it onto the exact HTN protocol.
    A device token is rejected here as a guard against accidentally placing a
    firmware credential in logs, queues, or the database through this API.
    """

    name: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    command_id: str = field(default_factory=lambda: uuid4().hex)

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("command name cannot be blank")
        forbidden = {"device_token", "deviceToken", "device-token"}
        if forbidden.intersection(self.payload):
            raise ValueError("A badge device token must never be supplied to the app gateway")


@dataclass(frozen=True, slots=True)
class BadgeEvent:
    """An input/state message originating from one badge."""

    badge_id: str
    event_type: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if not self.badge_id or not self.badge_id.strip():
            raise ValueError("event badge_id cannot be blank")
        if not self.event_type or not self.event_type.strip():
            raise ValueError("event_type cannot be blank")


@dataclass(frozen=True, slots=True)
class CommandReceipt:
    """Completion information for a command accepted by a transport."""

    badge_id: str
    command_id: str
    sent_at: datetime
    transport_result: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CommandTicket:
    """A queued command plus its awaitable eventual delivery result."""

    badge_id: str
    command_id: str
    _completion: asyncio.Future[CommandReceipt] = field(repr=False, compare=False)

    async def wait(self, timeout: float | None = None) -> CommandReceipt:
        """Wait for the transport to accept the command.

        A timeout affects only this caller; the queue worker continues trying
        to send the command unless the badge is explicitly unregistered.
        """

        waitable = asyncio.shield(self._completion)
        if timeout is None:
            return await waitable
        return await asyncio.wait_for(waitable, timeout)


class BadgeTransport(Protocol):
    """Service adapter used by :class:`HTNBadgeGateway`.

    Implementations must not require a firmware device token.  ``events`` is
    expected to be a long-running async iterator and should raise
    :class:`TransportError` when reconnecting is appropriate.
    """

    async def connect(self, credentials: BadgeCredentials) -> None:
        """Prepare a transport session for a badge."""

    async def send_command(
        self, credentials: BadgeCredentials, command: BadgeCommand
    ) -> Mapping[str, Any] | None:
        """Send one command and return optional service acknowledgement data."""

    async def events(self, credentials: BadgeCredentials) -> AsyncIterator[BadgeEvent]:
        """Yield events for ``credentials.badge_id`` until disconnected."""

    async def close(self, badge_id: str) -> None:
        """Close resources associated with one badge."""


class FixedRateLimiter:
    """A smooth, per-instance limiter that never bursts above the configured rate."""

    def __init__(self, commands_per_second: float = 20.0) -> None:
        if commands_per_second <= 0:
            raise ValueError("commands_per_second must be greater than zero")
        self._interval = 1.0 / commands_per_second
        self._next_send_at = 0.0
        self._lock = asyncio.Lock()

    async def wait_turn(self) -> None:
        """Wait until this badge may receive its next command."""

        async with self._lock:
            now = monotonic()
            scheduled = max(now, self._next_send_at)
            self._next_send_at = scheduled + self._interval
        delay = scheduled - monotonic()
        if delay > 0:
            await asyncio.sleep(delay)


@dataclass(slots=True)
class _QueuedCommand:
    command: BadgeCommand
    completion: asyncio.Future[CommandReceipt]


@dataclass(slots=True)
class _BadgeSession:
    credentials: BadgeCredentials
    queue: asyncio.Queue[_QueuedCommand]
    limiter: FixedRateLimiter
    command_task: asyncio.Task[None] | None = None
    event_task: asyncio.Task[None] | None = None
    closing: bool = False


@dataclass(frozen=True, slots=True)
class _HandlerRegistration:
    handler: EventHandler
    event_type: str | None
    badge_id: str | None


class HTNBadgeGateway:
    """Coordinates application commands and events for many Wi-Fi badges.

    The gateway is intentionally session-only.  Register a badge after the
    persistence layer has looked up its encrypted app key, and unregister it
    when the player logs out or its server-side session ends.

    Event handlers are invoked sequentially for each badge, preserving button
    order.  Different badges have independent event and command tasks.
    """

    def __init__(
        self,
        transport: BadgeTransport,
        *,
        commands_per_second: float = 20.0,
        queue_size: int = 128,
        reconnect_delay: float = 2.0,
    ) -> None:
        if queue_size <= 0:
            raise ValueError("queue_size must be greater than zero")
        if reconnect_delay <= 0:
            raise ValueError("reconnect_delay must be greater than zero")
        self._transport = transport
        self._commands_per_second = commands_per_second
        self._queue_size = queue_size
        self._reconnect_delay = reconnect_delay
        self._sessions: dict[str, _BadgeSession] = {}
        self._handlers: dict[str, _HandlerRegistration] = {}
        self._sessions_lock = asyncio.Lock()
        self._closed = False

    @property
    def registered_badge_ids(self) -> tuple[str, ...]:
        """A snapshot of currently registered badge IDs, without credentials."""

        return tuple(self._sessions)

    async def register_badge(self, credentials: BadgeCredentials) -> None:
        """Open independent queue/event processing for a badge.

        It is safe to call this again with the exact same credentials.  A
        conflicting app key is rejected instead of silently switching an
        active user's session.
        """

        if self._closed:
            raise GatewayError("gateway is closed")
        async with self._sessions_lock:
            existing = self._sessions.get(credentials.badge_id)
            if existing:
                if existing.credentials != credentials:
                    raise BadgeAlreadyRegisteredError(
                        f"Badge {credentials.badge_id!r} is already registered with another app key"
                    )
                return
            session = _BadgeSession(
                credentials=credentials,
                queue=asyncio.Queue(maxsize=self._queue_size),
                limiter=FixedRateLimiter(self._commands_per_second),
            )
            self._sessions[credentials.badge_id] = session

        try:
            await self._transport.connect(credentials)
        except GatewayError:
            async with self._sessions_lock:
                self._sessions.pop(credentials.badge_id, None)
            raise
        except Exception as exc:
            # Third-party WebSocket clients expose their own connection
            # exceptions (for example ``websockets.InvalidStatus`` for an
            # HTTP 502).  Keep that implementation detail inside the gateway:
            # callers need a recoverable GatewayError so one unavailable badge
            # cannot abort the whole FastAPI lifespan before configured keys
            # from .env have a chance to replace stale stored credentials.
            async with self._sessions_lock:
                self._sessions.pop(credentials.badge_id, None)
            raise TransportError(
                f"Could not open the badge service connection for {credentials.badge_id!r} "
                f"({type(exc).__name__})"
            ) from exc

        # Creating tasks only after a successful connect avoids a background
        # reconnect loop for a badge that was never actually registered.
        session.command_task = asyncio.create_task(
            self._command_worker(session), name=f"htn-command-{credentials.badge_id}"
        )
        session.event_task = asyncio.create_task(
            self._event_worker(session), name=f"htn-events-{credentials.badge_id}"
        )

    async def unregister_badge(self, badge_id: str) -> None:
        """Stop a badge session and fail any commands not yet delivered."""

        async with self._sessions_lock:
            session = self._sessions.pop(badge_id, None)
        if session is None:
            return

        session.closing = True
        tasks = [task for task in (session.command_task, session.event_task) if task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._fail_pending(session, GatewayError(f"Badge {badge_id!r} was unregistered"))
        try:
            await self._transport.close(badge_id)
        except Exception as exc:  # A shutdown failure should not strand queued futures.
            LOGGER.warning("Could not close transport for badge %s: %s", badge_id, exc)

    async def close(self) -> None:
        """Close every live session.  Safe to call more than once."""

        if self._closed:
            return
        self._closed = True
        for badge_id in tuple(self._sessions):
            await self.unregister_badge(badge_id)

    async def enqueue(self, badge_id: str, command: BadgeCommand) -> CommandTicket:
        """Place a command on that badge's outgoing queue without waiting for I/O."""

        session = self._sessions.get(badge_id)
        if session is None or session.closing:
            raise BadgeNotRegisteredError(f"Badge {badge_id!r} is not registered")
        completion: asyncio.Future[CommandReceipt] = asyncio.get_running_loop().create_future()
        pending = _QueuedCommand(command=command, completion=completion)
        try:
            session.queue.put_nowait(pending)
        except asyncio.QueueFull as exc:
            raise BadgeQueueFullError(
                f"Outgoing command queue for badge {badge_id!r} is full"
            ) from exc
        return CommandTicket(badge_id, command.command_id, completion)

    async def send(
        self, badge_id: str, command: BadgeCommand, *, timeout: float | None = None
    ) -> CommandReceipt:
        """Queue a command and wait until the transport accepts it."""

        return await (await self.enqueue(badge_id, command)).wait(timeout)

    def add_event_handler(
        self,
        handler: EventHandler,
        *,
        event_type: str | None = None,
        badge_id: str | None = None,
    ) -> Callable[[], None]:
        """Register an event handler and return an unsubscribe function.

        ``event_type`` and ``badge_id`` are optional filters.  Handlers are
        run in registration order; an exception in one handler is logged and
        does not block the rest or disconnect the badge.
        """

        if not callable(handler):
            raise TypeError("handler must be callable")
        registration_id = uuid4().hex
        self._handlers[registration_id] = _HandlerRegistration(handler, event_type, badge_id)

        def unsubscribe() -> None:
            self._handlers.pop(registration_id, None)

        return unsubscribe

    async def dispatch_event(self, event: BadgeEvent) -> bool:
        """Dispatch an event, including synthetic events in tests.

        Returns ``False`` for events from an unregistered badge.  This guards
        against a transport accidentally delivering an event to the wrong user
        session.
        """

        if event.badge_id not in self._sessions:
            LOGGER.warning("Ignoring event from unregistered badge %s", event.badge_id)
            return False
        handlers = tuple(self._handlers.values())
        for registration in handlers:
            if registration.badge_id is not None and registration.badge_id != event.badge_id:
                continue
            if registration.event_type is not None and registration.event_type != event.event_type:
                continue
            try:
                outcome = registration.handler(event)
                if inspect.isawaitable(outcome):
                    await outcome
            except Exception:
                # Never interpolate payloads; an incoming event could contain
                # user-provided data and should not become an accidental log.
                LOGGER.exception(
                    "Badge event handler failed for badge %s event %s",
                    event.badge_id,
                    event.event_type,
                )
        return True

    async def _command_worker(self, session: _BadgeSession) -> None:
        current: _QueuedCommand | None = None
        try:
            while True:
                current = await session.queue.get()
                try:
                    await session.limiter.wait_turn()
                    result = await self._transport.send_command(session.credentials, current.command)
                    receipt = CommandReceipt(
                        badge_id=session.credentials.badge_id,
                        command_id=current.command.command_id,
                        sent_at=datetime.now(timezone.utc),
                        transport_result=dict(result or {}),
                    )
                    if not current.completion.done():
                        current.completion.set_result(receipt)
                except asyncio.CancelledError:
                    if not current.completion.done():
                        current.completion.set_exception(
                            GatewayError(f"Badge {session.credentials.badge_id!r} was unregistered")
                        )
                    raise
                except Exception as exc:
                    if not current.completion.done():
                        current.completion.set_exception(exc)
                finally:
                    session.queue.task_done()
                    current = None
        except asyncio.CancelledError:
            if current is not None and not current.completion.done():
                current.completion.set_exception(
                    GatewayError(f"Badge {session.credentials.badge_id!r} command worker stopped")
                )
            raise

    async def _event_worker(self, session: _BadgeSession) -> None:
        """Forward an event stream and reconnect after transient service failures."""

        badge_id = session.credentials.badge_id
        while not session.closing:
            try:
                stream_had_event = False
                async for event in self._transport.events(session.credentials):
                    stream_had_event = True
                    if event.badge_id != badge_id:
                        LOGGER.warning("Ignoring mismatched event for badge %s", badge_id)
                        continue
                    await self.dispatch_event(event)
                if session.closing:
                    return
                # A cleanly-ended stream is still a disconnect.  Treat it like
                # an exception so it cannot spin the event loop at full speed.
                if not stream_had_event:
                    raise TransportError("Badge event stream ended")
                raise TransportError("Badge event stream disconnected")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if session.closing:
                    return
                LOGGER.warning("Event stream lost for badge %s: %s", badge_id, exc)
                await self.dispatch_event(
                    BadgeEvent(badge_id, "transport_error", {"message": str(exc)})
                )
                await asyncio.sleep(self._reconnect_delay)
                try:
                    await self._transport.close(badge_id)
                    await self._transport.connect(session.credentials)
                    await self.dispatch_event(BadgeEvent(badge_id, "transport_reconnected"))
                except asyncio.CancelledError:
                    raise
                except Exception as reconnect_error:
                    LOGGER.warning("Reconnect failed for badge %s: %s", badge_id, reconnect_error)

    @staticmethod
    def _fail_pending(session: _BadgeSession, error: Exception) -> None:
        while True:
            try:
                pending = session.queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if not pending.completion.done():
                pending.completion.set_exception(error)
            session.queue.task_done()


@dataclass(frozen=True, slots=True)
class RecordedCommand:
    """A command observed by :class:`InMemoryBadgeTransport` during a test."""

    badge_id: str
    command: BadgeCommand
    sent_at: datetime


class InMemoryBadgeTransport:
    """Deterministic fake transport for unit tests and local UI work.

    Tests inject a button event with ``await transport.emit(...)`` and inspect
    ``transport.commands`` or await ``transport.next_command(...)``.  It never
    opens a network connection and never needs real credentials.
    """

    def __init__(self) -> None:
        self.commands: list[RecordedCommand] = []
        self.connected_badges: set[str] = set()
        self._events: dict[str, asyncio.Queue[BadgeEvent]] = {}
        self._command_available = asyncio.Event()
        self._closed = False

    async def connect(self, credentials: BadgeCredentials) -> None:
        if self._closed:
            raise TransportError("In-memory transport is closed")
        self.connected_badges.add(credentials.badge_id)
        self._events.setdefault(credentials.badge_id, asyncio.Queue())

    async def send_command(
        self, credentials: BadgeCredentials, command: BadgeCommand
    ) -> Mapping[str, Any]:
        if credentials.badge_id not in self.connected_badges:
            raise TransportError(f"Badge {credentials.badge_id!r} is not connected")
        self.commands.append(
            RecordedCommand(credentials.badge_id, command, datetime.now(timezone.utc))
        )
        self._command_available.set()
        return {"accepted": True, "command_id": command.command_id}

    async def events(self, credentials: BadgeCredentials) -> AsyncIterator[BadgeEvent]:
        queue = self._events.setdefault(credentials.badge_id, asyncio.Queue())
        while True:
            yield await queue.get()

    async def close(self, badge_id: str) -> None:
        self.connected_badges.discard(badge_id)

    async def emit(
        self, badge_id: str, event_type: str, payload: Mapping[str, Any] | None = None
    ) -> None:
        """Inject an event as though it had been sent by a badge."""

        queue = self._events.setdefault(badge_id, asyncio.Queue())
        await queue.put(BadgeEvent(badge_id, event_type, dict(payload or {})))

    async def next_command(self, timeout: float | None = 1.0) -> RecordedCommand:
        """Wait for and return the next not-yet-observed recorded command."""

        if not self.commands:
            waitable = self._command_available.wait()
            if timeout is None:
                await waitable
            else:
                await asyncio.wait_for(waitable, timeout)
        if not self.commands:
            raise TimeoutError("No command was recorded")
        command = self.commands.pop(0)
        if not self.commands:
            self._command_available.clear()
        return command


class RestBadgeTransport:
    """Simple REST command transport with no third-party dependency.

    This adapter deliberately does not invent a production HTN endpoint.  Pass
    the documented ``command_url_template`` during runtime integration, e.g.
    ``"https://service.example/badges/{badge_id}/commands"``.  The app key is
    sent as one configurable HTTP header; no firmware device token is used.
    REST has no incoming event stream, so pair it with the service's event
    transport when interactive button input is needed.
    """

    def __init__(
        self,
        command_url_template: str,
        *,
        app_key_header: str = "X-App-Key",
        extra_headers: Mapping[str, str] | None = None,
        timeout: float = 10.0,
    ) -> None:
        if "{badge_id}" not in command_url_template:
            raise ValueError("command_url_template must include '{badge_id}'")
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        self._command_url_template = command_url_template
        self._app_key_header = app_key_header
        self._extra_headers = dict(extra_headers or {})
        self._timeout = timeout
        self._closed_badges: set[str] = set()

    async def connect(self, credentials: BadgeCredentials) -> None:
        self._closed_badges.discard(credentials.badge_id)

    async def send_command(
        self, credentials: BadgeCredentials, command: BadgeCommand
    ) -> Mapping[str, Any]:
        if credentials.badge_id in self._closed_badges:
            raise TransportError(f"Badge {credentials.badge_id!r} REST session is closed")
        url = self._command_url_template.format(badge_id=quote(credentials.badge_id, safe=""))
        body = {
            "command_id": command.command_id,
            "command": command.name,
            "payload": dict(command.payload),
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            self._app_key_header: credentials.app_key,
            **self._extra_headers,
        }
        return await asyncio.to_thread(self._post_json, url, body, headers)

    async def events(self, credentials: BadgeCredentials) -> AsyncIterator[BadgeEvent]:
        # Keep a cancellable no-op iterator alive.  The gateway can therefore
        # use REST-only output while another component injects/dispatches
        # events, without a busy reconnection loop.
        await asyncio.Event().wait()
        if False:  # pragma: no cover - establishes this as an async generator.
            yield BadgeEvent(credentials.badge_id, "unreachable")

    async def close(self, badge_id: str) -> None:
        self._closed_badges.add(badge_id)

    def _post_json(
        self, url: str, body: Mapping[str, Any], headers: Mapping[str, str]
    ) -> Mapping[str, Any]:
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        request = Request(url, data=data, headers=dict(headers), method="POST")
        try:
            with urlopen(request, timeout=self._timeout) as response:  # noqa: S310 - caller configures URL.
                raw = response.read().decode("utf-8")
                if not raw:
                    return {"status": response.status}
                try:
                    decoded = json.loads(raw)
                except json.JSONDecodeError:
                    return {"status": response.status, "body": raw}
                if isinstance(decoded, Mapping):
                    return dict(decoded)
                return {"status": response.status, "body": decoded}
        except HTTPError as exc:
            raise TransportError(f"Badge service returned HTTP {exc.code}") from exc
        except URLError as exc:
            raise TransportError("Could not reach badge service") from exc
        except OSError as exc:
            raise TransportError("Badge service request failed") from exc


def websockets_available() -> bool:
    """Return whether the optional ``websockets`` package can be imported."""

    try:
        __import__("websockets")
    except ModuleNotFoundError:
        return False
    return True


class WebSocketBadgeTransport:
    """Configurable JSON-over-WebSocket transport.

    The built-in defaults are neutral JSON envelopes, not a claim about the
    final HTN production protocol.  Pass custom ``command_encoder``,
    ``event_decoder``, headers, and an optional connection payload after the
    exact HTN OS service protocol is confirmed.  When configured, the app key
    is sent in a header and is never included in event data or logging.
    """

    def __init__(
        self,
        url_template: str,
        *,
        app_key_header: str | None = "X-App-Key",
        extra_headers: Mapping[str, str] | None = None,
        command_encoder: CommandEncoder | None = None,
        event_decoder: EventDecoder | None = None,
        connect_payload_factory: ConnectPayloadFactory | None = None,
        open_timeout: float = 10.0,
    ) -> None:
        if "{badge_id}" not in url_template:
            raise ValueError("url_template must include '{badge_id}'")
        if open_timeout <= 0:
            raise ValueError("open_timeout must be greater than zero")
        self._url_template = url_template
        self._app_key_header = app_key_header
        self._extra_headers = dict(extra_headers or {})
        self._command_encoder = command_encoder or self._default_command_encoder
        self._event_decoder = event_decoder or self._default_event_decoder
        self._connect_payload_factory = connect_payload_factory
        self._open_timeout = open_timeout
        self._sockets: dict[str, Any] = {}
        self._send_locks: dict[str, asyncio.Lock] = {}
        self._connection_lock = asyncio.Lock()

    async def connect(self, credentials: BadgeCredentials) -> None:
        async with self._connection_lock:
            if credentials.badge_id in self._sockets:
                return
            websockets = self._load_websockets()
            # ``{app_key}`` is optional for generic transports.  It exists so
            # the concrete HTN service transport can use its documented query
            # parameter without putting the key into a JSON command frame.
            url = self._url_template.format(
                badge_id=quote(credentials.badge_id, safe=""),
                app_key=quote(credentials.app_key, safe=""),
            )
            headers = dict(self._extra_headers)
            if self._app_key_header:
                headers[self._app_key_header] = credentials.app_key
            connector = websockets.connect
            try:
                socket = await connector(
                    url, additional_headers=headers, open_timeout=self._open_timeout
                )
            except TypeError:
                # ``additional_headers`` is the newer spelling.  Supporting
                # ``extra_headers`` keeps the adapter compatible with widely
                # used pre-v14 websockets releases.
                socket = await connector(url, extra_headers=headers, open_timeout=self._open_timeout)
            self._sockets[credentials.badge_id] = socket
            self._send_locks[credentials.badge_id] = asyncio.Lock()

        payload = self._connect_payload_factory(credentials) if self._connect_payload_factory else None
        if payload:
            await socket.send(json.dumps(dict(payload), separators=(",", ":")))

    async def send_command(
        self, credentials: BadgeCredentials, command: BadgeCommand
    ) -> Mapping[str, Any]:
        socket = self._sockets.get(credentials.badge_id)
        lock = self._send_locks.get(credentials.badge_id)
        if socket is None or lock is None:
            raise TransportError(f"Badge {credentials.badge_id!r} WebSocket is not connected")
        message = self._command_encoder(credentials, command)
        async with lock:
            try:
                await socket.send(message)
            except Exception as exc:
                raise TransportError("Could not send WebSocket command") from exc
        return {"accepted": True, "command_id": command.command_id}

    async def events(self, credentials: BadgeCredentials) -> AsyncIterator[BadgeEvent]:
        socket = self._sockets.get(credentials.badge_id)
        if socket is None:
            raise TransportError(f"Badge {credentials.badge_id!r} WebSocket is not connected")
        try:
            async for raw_message in socket:
                event = self._event_decoder(credentials.badge_id, raw_message)
                if event is not None:
                    yield event
        except Exception as exc:
            raise TransportError("Badge WebSocket event stream failed") from exc

    async def close(self, badge_id: str) -> None:
        async with self._connection_lock:
            socket = self._sockets.pop(badge_id, None)
            self._send_locks.pop(badge_id, None)
        if socket is not None:
            try:
                await socket.close()
            except Exception as exc:
                raise TransportError("Could not close badge WebSocket") from exc

    @staticmethod
    def _load_websockets() -> Any:
        try:
            import websockets
        except ModuleNotFoundError as exc:
            raise OptionalWebsocketsDependencyError(
                "WebSocket support is optional. Install it with: py -m pip install websockets"
            ) from exc
        return websockets

    @staticmethod
    def _default_command_encoder(
        credentials: BadgeCredentials, command: BadgeCommand
    ) -> str:
        # credentials is intentionally unused: keep app keys in headers, not
        # in JSON command payloads where they are easier to leak.
        del credentials
        return json.dumps(
            {
                "type": "command",
                "command_id": command.command_id,
                "command": command.name,
                "payload": dict(command.payload),
            },
            separators=(",", ":"),
        )

    @staticmethod
    def _default_event_decoder(badge_id: str, raw_message: str | bytes) -> BadgeEvent:
        if isinstance(raw_message, bytes):
            raw_message = raw_message.decode("utf-8", errors="replace")
        try:
            decoded = json.loads(raw_message)
        except json.JSONDecodeError:
            return BadgeEvent(badge_id, "message", {"raw": raw_message})
        if not isinstance(decoded, Mapping):
            return BadgeEvent(badge_id, "message", {"value": decoded})
        event_type = str(decoded.get("event") or decoded.get("type") or "message")
        payload = decoded.get("payload")
        if isinstance(payload, Mapping):
            normalized_payload: Mapping[str, Any] = dict(payload)
        elif payload is None:
            normalized_payload = {
                str(key): value
                for key, value in decoded.items()
                if key not in {"badge_id", "badgeId", "event", "type"}
            }
        else:
            normalized_payload = {"value": payload}
        # The gateway validates this ID before dispatching it.  Keeping the
        # service-supplied identifier here helps detect a wiring mistake.
        incoming_badge_id = str(decoded.get("badge_id") or decoded.get("badgeId") or badge_id)
        return BadgeEvent(incoming_badge_id, event_type, normalized_payload)


class HTNServiceWebSocketTransport(WebSocketBadgeTransport):
    """Concrete adapter for the public HTN OS app WebSocket protocol.

    It connects an application server to the HTN badge service at
    ``wss://badge.solana-htn.com/v1/badges/{badge_id}/ws?key=...``.  The key
    is URL-encoded only for the connection request; it is never put in an
    event, command body, logger call, or firmware device-token field.

    ``BadgeCommand`` names use the public endpoint names from the HTN OS docs:
    ``clear``, ``text``, ``rect``, ``image``, ``leds``, ``buttons``, ``accel``,
    ``accelStream``, ``nfc``, ``info``, and ``home``.  A few readable aliases
    (such as ``draw_text``) are accepted so existing server renderers do not
    need to know the wire spelling.

    The gateway's command receipt means that a frame was accepted by the local
    WebSocket library. The service may later emit a reply or error frame; those
    are intentionally not presented as input events because ``BadgeEvent`` is
    reserved for hardware/service events. A future reply-correlating layer can
    be added without changing the command format.
    """

    DEFAULT_SERVICE_BASE_URL = "wss://badge.solana-htn.com"
    SUPPORTED_COMMANDS = frozenset(
        {
            "clear",
            "text",
            "rect",
            "image",
            "leds",
            "buttons",
            "accel",
            "accelStream",
            "nfc",
            "info",
            "home",
        }
    )
    _COMMAND_ALIASES = {
        "draw_text": "text",
        "draw_rect": "rect",
        "draw_image": "image",
        "set_leds": "leds",
        "accelerometer": "accel",
        "accelerometer_stream": "accelStream",
        "accel_stream": "accelStream",
        "scan_nfc": "nfc",
        "go_home": "home",
    }

    def __init__(
        self,
        *,
        service_base_url: str = DEFAULT_SERVICE_BASE_URL,
        open_timeout: float = 10.0,
    ) -> None:
        base_url = service_base_url.rstrip("/")
        if not base_url.startswith(("ws://", "wss://")):
            raise ValueError("service_base_url must start with 'ws://' or 'wss://'")
        super().__init__(
            f"{base_url}/v1/badges/{{badge_id}}/ws?key={{app_key}}",
            # HTN OS documents the query parameter for the app WebSocket.
            # Unlike REST, no redundant header is needed.
            app_key_header=None,
            command_encoder=self._command_encoder,
            event_decoder=self.decode_event,
            open_timeout=open_timeout,
        )

    @classmethod
    def canonical_command_name(cls, name: str) -> str:
        """Return the official wire command name or raise a helpful error."""

        normalized = str(name).strip()
        canonical = cls._COMMAND_ALIASES.get(normalized, normalized)
        if canonical not in cls.SUPPORTED_COMMANDS:
            choices = ", ".join(sorted(cls.SUPPORTED_COMMANDS))
            raise ValueError(f"Unsupported HTN OS command {name!r}; expected one of {choices}.")
        return canonical

    @classmethod
    def encode_command(cls, command: BadgeCommand) -> str:
        """Encode one logical command as the exact HTN OS WebSocket envelope.

        ``leds`` is the only special case: its REST body is a discriminated
        union, so the WebSocket protocol nests that body under ``body``.  All
        other payload fields appear beside ``cmd`` and ``id`` exactly as the
        corresponding REST endpoint documents them.
        """

        command_name = cls.canonical_command_name(command.name)
        payload = dict(command.payload)
        reserved = {"cmd", "id"}.intersection(payload)
        if reserved:
            fields = ", ".join(sorted(reserved))
            raise ValueError(f"HTN OS command payload cannot override reserved field(s): {fields}")

        envelope: JsonObject = {"cmd": command_name, "id": command.command_id}
        if command_name == "leds":
            envelope["body"] = cls._led_body(payload)
        else:
            envelope.update(payload)
        return json.dumps(envelope, separators=(",", ":"))

    @classmethod
    def _command_encoder(
        cls, credentials: BadgeCredentials, command: BadgeCommand
    ) -> str:
        # The public app-WebSocket protocol authenticates the connection in
        # the URL query. Credentials must not be serialized again here.
        del credentials
        return cls.encode_command(command)

    @staticmethod
    def _led_body(payload: Mapping[str, Any]) -> JsonObject:
        """Normalize the LEDs union into the documented WebSocket ``body``."""

        if "body" in payload:
            if len(payload) != 1:
                raise ValueError("The LEDs command cannot mix 'body' with other payload fields.")
            body = payload["body"]
            if not isinstance(body, Mapping):
                raise ValueError("The LEDs command 'body' must be an object.")
            return dict(body)

        # The common server-renderer spelling is ``colors``. Convert it to
        # the service's public ``leds`` field and leave unspecified LEDs alone.
        if "colors" in payload:
            colors = payload["colors"]
            if not isinstance(colors, (list, tuple)):
                raise ValueError("The LEDs command 'colors' must be a list or tuple.")
            if len(colors) > 6:
                raise ValueError("The HTN OS badge has exactly six LEDs.")
            if any(key != "colors" and key != "brightness" for key in payload):
                raise ValueError("The LEDs command 'colors' cannot be mixed with LED body fields.")
            # HTN OS intentionally controls LED brightness in firmware; the
            # renderer's optional brightness is therefore not a wire field.
            return {"leds": [*colors, *([None] * (6 - len(colors)))]}

        return dict(payload)

    @staticmethod
    def decode_event(badge_id: str, raw_message: str | bytes) -> BadgeEvent | None:
        """Decode an HTN OS ``type:event`` frame into a :class:`BadgeEvent`.

        Reply/error frames are command-plane information rather than badge
        input, so this event stream ignores them. The surrounding gateway
        already verifies that the returned ``badgeId`` matches the registered
        session before it dispatches the event.
        """

        if isinstance(raw_message, bytes):
            try:
                raw_message = raw_message.decode("utf-8")
            except UnicodeDecodeError:
                return None
        try:
            message = json.loads(raw_message)
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(message, Mapping) or message.get("type") != "event":
            return None
        data = message.get("data")
        if not isinstance(data, Mapping):
            return None
        event_type = data.get("event")
        if not isinstance(event_type, str) or not event_type.strip():
            return None
        source_badge_id = data.get("badgeId")
        if not isinstance(source_badge_id, str) or not source_badge_id.strip():
            source_badge_id = badge_id
        payload = {
            str(key): value
            for key, value in data.items()
            if key not in {"event", "badgeId"}
        }
        return BadgeEvent(source_badge_id, event_type.strip(), payload)


def htn_command_from_operation(
    operation: Mapping[str, Any], *, command_id: str | None = None
) -> BadgeCommand:
    """Map a basic declarative UI operation into an HTN OS command.

    This is intentionally a small bridge between :mod:`badge_ui` operations
    and the badge service, not a layout engine. Unsupported local-only fields
    such as rounded corners, strokes, word wrap, or scrolling are ignored; the
    app runtime can realize those with several basic commands when required.
    ``image`` accepts an already-base64 image or a ``data:image/...;base64,``
    source. Network URLs are rejected because the HTN service expects image
    bytes, not a URL it should fetch.
    """

    if not isinstance(operation, Mapping):
        raise TypeError("operation must be a mapping")
    raw_name = operation.get("op")
    if not isinstance(raw_name, str) or not raw_name.strip():
        raise ValueError("operation needs a non-empty 'op' field")
    command_name = HTNServiceWebSocketTransport.canonical_command_name(raw_name)

    def required(*names: str) -> None:
        missing = [name for name in names if name not in operation]
        if missing:
            raise ValueError(f"{command_name!r} operation is missing: {', '.join(missing)}")

    if command_name == "clear":
        payload = {"color": operation["color"]} if "color" in operation else {}
    elif command_name == "rect":
        required("x", "y")
        width_key = "w" if "w" in operation else "width"
        height_key = "h" if "h" in operation else "height"
        color_key = "color" if "color" in operation else "fill"
        required(width_key, height_key, color_key)
        payload = {
            "x": operation["x"],
            "y": operation["y"],
            "w": operation[width_key],
            "h": operation[height_key],
            "color": operation[color_key],
        }
    elif command_name == "text":
        required("text", "x", "y")
        payload = {key: operation[key] for key in ("text", "x", "y", "color", "background", "clear") if key in operation}
        if "size" in operation:
            payload["size"] = _htn_font_size(operation["size"])
    elif command_name == "image":
        source = operation.get("image", operation.get("source"))
        if not isinstance(source, str) or not source:
            raise ValueError("image operation needs a non-empty base64 'image' or 'source'")
        if source.startswith("data:image/"):
            marker = ";base64,"
            if marker not in source:
                raise ValueError("image data URLs must use base64 encoding")
            source = source.split(marker, 1)[1]
        elif source.startswith(("http://", "https://", "/")):
            raise ValueError("HTN OS image commands require base64 image data, not a URL or path")
        payload = {"image": source}
        for key in ("x", "y", "fit"):
            if key in operation:
                payload[key] = operation[key]
    elif command_name == "leds":
        if "body" in operation:
            payload = {"body": operation["body"]}
        elif "all" in operation or "leds" in operation:
            payload = {key: operation[key] for key in ("all", "leds") if key in operation}
        elif "colors" in operation:
            payload = {"colors": operation["colors"]}
            if "brightness" in operation:
                payload["brightness"] = operation["brightness"]
        else:
            raise ValueError("leds operation needs 'all', 'leds', 'colors', or 'body'")
    else:
        # The remaining endpoint names already use their public body field
        # spelling (e.g. accelStream's ``hz`` and NFC's ``timeoutMs``).
        payload = {
            str(key): value
            for key, value in operation.items()
            if key not in {"op", "cmd", "id"}
        }

    if command_id is None:
        return BadgeCommand(command_name, payload)
    return BadgeCommand(command_name, payload, command_id=command_id)


def _htn_font_size(value: Any) -> int:
    """Translate a logical pixel-ish text size to HTN OS bitmap font scales."""

    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError("text operation size must be a positive integer")
    if value <= 4:
        return value
    for pixels, scale in ((12, 1), (16, 2), (24, 3)):
        if value <= pixels:
            return scale
    return 4


async def _self_test() -> None:
    """Run a no-network demonstration used by ``--self-test``."""

    # Protocol-only checks: these do not need the optional websockets package
    # or a real badge/app key.
    encoded_text = json.loads(
        HTNServiceWebSocketTransport.encode_command(
            BadgeCommand(
                "text",
                {"text": "PONG", "x": 100, "y": 100, "size": 4},
                command_id="text-1",
            )
        )
    )
    assert encoded_text == {
        "cmd": "text",
        "id": "text-1",
        "text": "PONG",
        "x": 100,
        "y": 100,
        "size": 4,
    }
    encoded_leds = json.loads(
        HTNServiceWebSocketTransport.encode_command(
            BadgeCommand("leds", {"all": "#220022"}, command_id="led-1")
        )
    )
    assert encoded_leds == {"cmd": "leds", "id": "led-1", "body": {"all": "#220022"}}
    decoded_event = HTNServiceWebSocketTransport.decode_event(
        "demo-badge",
        '{"type":"event","data":{"event":"button","badgeId":"demo-badge",'
        '"button":"a","pressed":true}}',
    )
    assert decoded_event is not None
    assert decoded_event.event_type == "button"
    assert decoded_event.payload == {"button": "a", "pressed": True}
    assert HTNServiceWebSocketTransport.decode_event(
        "demo-badge", '{"type":"reply","id":"text-1","data":{"ok":true}}'
    ) is None
    mapped_rect = htn_command_from_operation(
        {"op": "rect", "x": 1, "y": 2, "width": 3, "height": 4, "fill": "#fff"},
        command_id="rect-1",
    )
    assert json.loads(HTNServiceWebSocketTransport.encode_command(mapped_rect)) == {
        "cmd": "rect",
        "id": "rect-1",
        "x": 1,
        "y": 2,
        "w": 3,
        "h": 4,
        "color": "#fff",
    }

    transport = InMemoryBadgeTransport()
    gateway = HTNBadgeGateway(transport, commands_per_second=1000)
    seen_button = asyncio.Event()
    received_events: list[BadgeEvent] = []

    async def on_button(event: BadgeEvent) -> None:
        received_events.append(event)
        seen_button.set()

    gateway.add_event_handler(on_button, event_type="button")
    await gateway.register_badge(BadgeCredentials("demo-badge", "demo-app-key"))
    receipt = await gateway.send(
        "demo-badge", BadgeCommand("draw_text", {"text": "Shutterdex"}), timeout=1
    )
    assert receipt.command_id
    recorded = await transport.next_command()
    assert recorded.command.name == "draw_text"
    await transport.emit("demo-badge", "button", {"button": "A", "pressed": True})
    await asyncio.wait_for(seen_button.wait(), timeout=1)
    assert received_events[0].payload["button"] == "A"
    await gateway.close()
    print("HTN gateway self-test passed")


def _main() -> None:
    parser = argparse.ArgumentParser(description="HTN badge gateway utility")
    parser.add_argument("--self-test", action="store_true", help="run an in-memory smoke test")
    args = parser.parse_args()
    if args.self_test:
        asyncio.run(_self_test())
    else:
        parser.print_help()


if __name__ == "__main__":
    _main()
