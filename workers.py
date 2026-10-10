# -*- coding: utf-8 -*-
"""Qt workers: the Nexus bridge checker, plain-HTTP link checker, MO2
download runner, and the online half of the preflight."""

from __future__ import annotations

import os
import re
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

import mobase
from PyQt6.QtCore import QObject, QSettings, QThread, QTimer, QUrl, pyqtSignal
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from . import preflight
from .common import Finding, SEV_INFO, free_bytes, human_size
from .model import (
    FLAGGED_STATES,
    KIND_DIRECT,
    KIND_MANUAL,
    KIND_NEXUS,
    ST_DL_FAILED,
    ST_DOWNLOADED,
    ST_DOWNLOADING,
    ST_ERROR,
    ST_FILE_GONE,
    ST_MOD_GONE,
    ST_OK,
    Entry,
    archive_missing,
    plan_requests,
)

USER_AGENT = "MO2-WabbajackAuthorToolkit/2.0"

# ---------------------------------------------------------------------------
# Nexus bridge
# ---------------------------------------------------------------------------

_NOT_FOUND_PATTERNS = ("404", "not found", "no such", "does not exist")
_FORBIDDEN_PATTERNS = ("403", "forbidden", "unauthor")
_RATE_LIMIT_PATTERNS = ("429", "rate limit", "too many requests")

# Nexus file categories: 1 main, 2 update, 3 optional, 4 old, 5 misc, 6 deleted, 7 archived
NEXUS_CATEGORY_DELETED = 6
NEXUS_CATEGORY_ARCHIVED = 7


def _split_signal_args(args: Sequence[Any]) -> Tuple[List[str], List[int], List[Any]]:
    strings = [a for a in args if isinstance(a, str)]
    ints = [a for a in args if isinstance(a, int) and not isinstance(a, bool)]
    others = [a for a in args if not isinstance(a, (str, int, bool))]
    return strings, ints, others


def _classify_error(message: str) -> Tuple[str, str]:
    lowered = (message or "").lower()
    if any(p in lowered for p in _RATE_LIMIT_PATTERNS):
        return ST_ERROR, f"Nexus rate limit hit ({message})"
    if any(p in lowered for p in _NOT_FOUND_PATTERNS):
        return ST_MOD_GONE, "Nexus returns not-found - deleted or hidden"
    if any(p in lowered for p in _FORBIDDEN_PATTERNS):
        return ST_MOD_GONE, f"Access refused by Nexus - moderated or hidden ({message})"
    return ST_ERROR, message or "request failed"


Key = Tuple[str, int, Optional[int]]


class AvailabilityChecker(QObject):
    """Drives MO2's Nexus bridge over a queue of requests.

    filesAvailable (QList<ModRepositoryFileInfo*>) cannot be marshalled to
    Python, so everything goes through requestDescription() and
    requestFileInfo(), whose payloads arrive as dicts.

    Default plan: exactly one fileInfo request per unique Nexus file. A
    download that matches an installed mod is a single entry, so it costs one
    call. Mod pages are only requested when there is no file ID, when the
    author opts in, or (optionally) as a follow-up for a file that failed, to
    record *why* it is gone.
    """

    progressed = pyqtSignal(int, int)
    groupResolved = pyqtSignal(object)
    logged = pyqtSignal(str)
    debug = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(
        self,
        organizer: mobase.IOrganizer,
        entries: List[Entry],
        max_in_flight: int = 4,
        timeout_seconds: int = 45,
        query_status_on_failure: bool = True,
        verify_file_ids: bool = True,
        confirm_mod_pages: bool = False,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._entries = entries
        self._max_in_flight = max(1, max_in_flight)
        self._timeout = max(10, timeout_seconds)
        self._followup = query_status_on_failure
        self._stopping = False
        self._running = False
        self._user_data: Any = None
        self._user_data_known = False

        self._bridge = organizer.createNexusBridge()
        if self._bridge is None:
            raise RuntimeError("createNexusBridge() returned None - MO2 has no Nexus interface.")

        self._connected: List[str] = []
        self._sig_description = self._connect_signal("descriptionAvailable", self._on_description)
        self._sig_file_info = self._connect_signal("fileInfoAvailable", self._on_file_info)
        self._sig_failed = self._connect_signal("requestFailed", self._on_failed)
        if self._sig_failed is None:
            raise RuntimeError("The Nexus bridge exposes no usable 'requestFailed' signal on this MO2 build.")
        if self._sig_description is None and (confirm_mod_pages or not verify_file_ids):
            raise RuntimeError("The Nexus bridge exposes no usable 'descriptionAvailable' signal.")
        if verify_file_ids and self._sig_file_info is None:
            verify_file_ids = False
            self.logged.emit("This MO2 build cannot deliver per-file results to Python; "
                             "falling back to mod-page checks.")
        if self._sig_description is None:
            self._followup = False

        self._plan = plan_requests(entries, verify_file_ids, confirm_mod_pages)
        self._queue: List[Key] = self._plan.first_wave
        self._deferred: List[Key] = list(self._plan.deferred_files)
        self._inflight: Dict[Key, float] = {}
        self._followups: Set[Tuple[str, int]] = set()   # mod keys asked only for a reason
        self._mod_ok: Set[Tuple[str, int]] = set()
        self._done = 0
        self._total = self._plan.total_calls

        self._watchdog = QTimer(self)
        self._watchdog.setInterval(3000)
        self._watchdog.timeout.connect(self._check_timeouts)

    def _connect_signal(self, name: str, handler) -> Optional[str]:
        try:
            signal = getattr(self._bridge, name)
        except TypeError as exc:
            self.debug.emit(f"!! signal '{name}' unusable from Python: {exc}")
            return None
        except AttributeError:
            self.debug.emit(f"!! bridge has no signal '{name}'")
            return None
        try:
            signal.connect(handler)
        except Exception as exc:
            self.debug.emit(f"!! could not connect '{name}': {exc}")
            return None
        self._connected.append(name)
        return name

    @property
    def connected_signals(self) -> List[str]:
        return list(self._connected)

    @property
    def total_groups(self) -> int:
        return self._total

    # -- control -----------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        if not self._queue and not self._deferred:
            self._finish()
            return
        self._watchdog.start()
        self._pump()

    def stop(self) -> None:
        self._stopping = True
        self._queue.clear()
        self._deferred.clear()
        if not self._inflight:
            self._finish()

    def _finish(self) -> None:
        self._watchdog.stop()
        self._running = False
        self.finished.emit()

    # -- queue -------------------------------------------------------------

    def _send(self, key: Key, game: str) -> Tuple[bool, str]:
        candidates = [self._user_data] if self._user_data_known else [None, "", 0, []]
        errors: List[str] = []
        for user_data in candidates:
            try:
                if key[2] is None:
                    self._bridge.requestDescription(game, key[1], user_data)
                else:
                    self._bridge.requestFileInfo(game, key[1], key[2], user_data)
                if not self._user_data_known:
                    self._user_data, self._user_data_known = user_data, True
                    self.debug.emit(f"   bindings accept user_data={user_data!r}")
                return True, ""
            except (TypeError, ValueError) as exc:
                errors.append(f"user_data={user_data!r}: {exc}")
            except Exception as exc:  # pragma: no cover
                return False, str(exc)
        return False, " | ".join(errors) or "unknown error"

    def _pump(self) -> None:
        while not self._stopping and self._queue and len(self._inflight) < self._max_in_flight:
            key = self._queue.pop(0)
            if key in self._inflight:
                self._queue.append(key)
                break
            game = self._plan.game_of.get((key[0], key[1]), key[0])
            self._inflight[key] = time.monotonic()
            label = "description" if key[2] is None else f"fileInfo {key[2]}"
            self.debug.emit(f"-> {label}: game={game!r} modID={key[1]}")
            sent, error = self._send(key, game)
            if not sent:
                self._inflight.pop(key, None)
                self.debug.emit(f"!! bridge call rejected: {error}")
                self._resolve(key, ST_ERROR, f"bridge call failed: {error}")

        if not self._queue and not self._inflight:
            if self._deferred and not self._stopping:
                self._release_deferred()
            else:
                self._finish()

    def _release_deferred(self) -> None:
        ready = [k for k in self._deferred if (k[0], k[1]) in self._mod_ok]
        skipped = len(self._deferred) - len(ready)
        self._deferred = []
        if skipped:
            self._done += skipped
            self.progressed.emit(self._done, self._total)
        if not ready:
            self._finish()
            return
        self.logged.emit(f"Mod pages done. Verifying {len(ready)} file ID(s).")
        self._queue = ready
        self._pump()

    def _check_timeouts(self) -> None:
        now = time.monotonic()
        for key, started in list(self._inflight.items()):
            if now - started >= self._timeout:
                self._inflight.pop(key, None)
                if key[2] is None and (key[0], key[1]) in self._followups:
                    self._followups.discard((key[0], key[1]))
                    continue
                self._resolve(key, ST_ERROR, f"no response from Nexus within {self._timeout}s")
        self._pump()

    # -- resolution --------------------------------------------------------

    def _indices_for(self, key: Key) -> List[int]:
        if key[2] is None:
            return self._plan.mod_groups.get((key[0], key[1]), [])
        return self._plan.file_groups.get((key[0], key[1], key[2]), [])

    def _resolve(self, key: Key, status: str, reason: str, nexus_status: str = "",
                 keep_file_entries_pending: bool = False) -> None:
        indices = self._indices_for(key)
        for index in indices:
            entry = self._entries[index]
            if nexus_status:
                entry.nexus_status = nexus_status
            if keep_file_entries_pending and entry.file_id and (
                (key[0], key[1], entry.file_id) in self._plan.file_groups
            ):
                entry.reason = reason
                continue
            entry.status = status
            entry.reason = reason
        self._done += 1
        self.progressed.emit(self._done, self._total)
        if indices:
            self.groupResolved.emit(indices)

    def _annotate_followup(self, mod_key: Tuple[str, int], status_text: str) -> None:
        touched = []
        for index in self._plan.mod_groups.get(mod_key, []):
            entry = self._entries[index]
            if entry.status in FLAGGED_STATES:
                entry.nexus_status = status_text
                entry.reason += f" [mod page: {status_text}]"
                touched.append(index)
        if touched:
            self.groupResolved.emit(touched)

    # -- bridge signals ----------------------------------------------------

    def _dump(self, label: str, args: Sequence[Any]) -> None:
        parts = []
        for arg in args:
            text = repr(arg)
            parts.append(f"{type(arg).__name__}={text[:140] + ('...' if len(text) > 140 else '')}")
        self.debug.emit(f"<- {label}({len(args)} args): " + ", ".join(parts))

    def _match(self, strings: List[str], ints: List[int], want_file: Optional[bool]) -> Optional[Key]:
        if not ints:
            return None
        values = set(ints)
        games = {s.lower() for s in strings}
        fallback: Optional[Key] = None
        for key in self._inflight:
            if key[1] not in values:
                continue
            if key[2] is not None:
                if want_file is False:
                    continue
                if key[2] in values:
                    return key
                continue
            if want_file is True:
                continue
            if not games or key[0] in games:
                return key
            fallback = fallback or key
        return fallback

    @staticmethod
    def _payload(args: Sequence[Any]) -> Optional[dict]:
        for arg in args:
            if isinstance(arg, dict):
                return arg
        return None

    def _on_description(self, *args: Any) -> None:
        self._dump("descriptionAvailable", args)
        strings, ints, _ = _split_signal_args(args)
        key = self._match(strings, ints, want_file=False)
        if key is None or key not in self._inflight:
            self.debug.emit(f"   ignored (no in-flight match for {key!r})")
            return
        self._inflight.pop(key, None)
        mod_key = (key[0], key[1])

        payload = self._payload(args) or {}
        status_text = str(payload.get("status", "") or "")
        available = payload.get("available")
        unavailable = available is False or (
            status_text and status_text.lower() not in ("published", "public")
        )

        if mod_key in self._followups:
            self._followups.discard(mod_key)
            self._annotate_followup(mod_key, status_text or ("unavailable" if unavailable else "published"))
            self._pump()
            return

        if unavailable:
            self._resolve(key, ST_MOD_GONE, f"Nexus reports the mod as '{status_text or 'unavailable'}'",
                          nexus_status=status_text)
        else:
            self._mod_ok.add(mod_key)
            self._resolve(key, ST_OK, "Mod page available; file check queued"
                          if any(k[:2] == mod_key for k in self._deferred) else "Mod page available",
                          nexus_status=status_text, keep_file_entries_pending=True)
        self._pump()

    def _on_file_info(self, *args: Any) -> None:
        self._dump("fileInfoAvailable", args)
        strings, ints, _ = _split_signal_args(args)
        key = self._match(strings, ints, want_file=True)
        if key is None or key not in self._inflight:
            self.debug.emit(f"   ignored (no in-flight match for {key!r})")
            return
        self._inflight.pop(key, None)

        payload = self._payload(args)
        category = ""
        category_id = None
        if payload is not None:
            category = str(payload.get("category_name", "") or "")
            category_id = payload.get("category_id")

        if payload is not None and (
            payload.get("category_name", "x") is None or category_id == NEXUS_CATEGORY_DELETED
        ):
            self._resolve(key, ST_FILE_GONE, "File exists but Nexus no longer serves it (deleted category)")
            self._maybe_followup(key)
        elif category_id == NEXUS_CATEGORY_ARCHIVED:
            self._resolve(key, ST_OK, "File is archived on Nexus (hidden from the Files tab, still served)")
        else:
            suffix = f" ({category})" if category else ""
            self._resolve(key, ST_OK, f"File available{suffix}")
        self._pump()

    def _maybe_followup(self, key: Key) -> None:
        mod_key = (key[0], key[1])
        if (not self._followup or key[2] is None or mod_key in self._followups
                or mod_key in self._plan.need_description or self._stopping):
            return
        self._followups.add(mod_key)
        self._queue.append((key[0], key[1], None))

    def _on_failed(self, *args: Any) -> None:
        self._dump("requestFailed", args)
        strings, ints, _ = _split_signal_args(args)
        key = self._match(strings, ints, want_file=None)
        if key is None or key not in self._inflight:
            self.debug.emit(f"   ignored (no in-flight match for {key!r})")
            return
        self._inflight.pop(key, None)

        message = strings[-1] if len(strings) > 1 else (strings[0] if strings else "")
        mod_key = (key[0], key[1])
        if key[2] is None and mod_key in self._followups:
            self._followups.discard(mod_key)
            self._annotate_followup(mod_key, f"page request failed: {message}")
            self._pump()
            return

        if any(p in (message or "").lower() for p in _RATE_LIMIT_PATTERNS):
            self._resolve(key, ST_ERROR, f"Nexus rate limit hit ({message})")
            self.logged.emit("Nexus rate limit reached - stopping. Confirmed entries are cached, "
                             "so a later run picks up where this one left off.")
            self.stop()
            return

        status, reason = _classify_error(message)
        if key[2] is not None and status == ST_MOD_GONE:
            reason = f"File {key[2]} is no longer available on Nexus ({message})"
        self._resolve(key, status, reason)
        if status == ST_MOD_GONE:
            self._maybe_followup(key)
        self._pump()


# ---------------------------------------------------------------------------
# Non-Nexus link checking (directURL / manualURL)
# ---------------------------------------------------------------------------

WHITELIST_URL = (
    "https://raw.githubusercontent.com/wabbajack-tools/opt-out-lists/master/ServerWhitelist.yml"
)
OPAQUE_HOSTS = {
    "mega.nz": "MEGA serves the same page for live and deleted files",
    "mega.co.nz": "MEGA serves the same page for live and deleted files",
}
SOFT_HOSTS = {
    "drive.google.com": "Google Drive returns a landing/quota page rather than the file",
    "docs.google.com": "Google Drive returns a landing/quota page rather than the file",
    "www.mediafire.com": "MediaFire returns a landing page rather than the file",
    "mediafire.com": "MediaFire returns a landing page rather than the file",
    "www.dropbox.com": "Dropbox returns a landing page unless the link ends in dl=1",
    "dropbox.com": "Dropbox returns a landing page unless the link ends in dl=1",
    "disk.yandex.com": "Yandex returns a landing page rather than the file",
    "disk.yandex.ru": "Yandex returns a landing page rather than the file",
}


def _host_of(url: str) -> str:
    match = re.match(r"^\s*https?://([^/:?#]+)", url or "", re.IGNORECASE)
    return match.group(1).lower() if match else ""


def _request(url: str, timeout_s: int, ranged: bool = False) -> QNetworkRequest:
    request = QNetworkRequest(QUrl(url))
    request.setHeader(QNetworkRequest.KnownHeaders.UserAgentHeader,
                      f"Mozilla/5.0 (compatible; {USER_AGENT})")
    request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                         QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy)
    request.setTransferTimeout(timeout_s * 1000)
    if ranged:
        request.setRawHeader(b"Range", b"bytes=0-0")
    return request


class UrlChecker(QObject):
    """HEAD (or ranged GET) each directURL/manualURL; compare sizes where the
    archive is local; flag links missing from Wabbajack's server whitelist."""

    progressed = pyqtSignal(int, int)
    entryResolved = pyqtSignal(object)
    logged = pyqtSignal(str)
    debug = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self, entries: List[Entry], indices: List[int], max_in_flight: int = 4,
                 timeout_seconds: int = 30, check_whitelist: bool = True,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._entries = entries
        self._queue = list(indices)
        self._total = len(indices)
        self._done = 0
        self._max_in_flight = max(1, max_in_flight)
        self._timeout = max(5, timeout_seconds)
        self._check_whitelist = check_whitelist
        self._stopping = False
        self._finished = False
        self._manager = QNetworkAccessManager(self)
        self._inflight: Dict[Any, Tuple[int, bool]] = {}
        self._whitelist: Optional[List[str]] = None
        self._watchdog = QTimer(self)
        self._watchdog.setInterval(2000)
        self._watchdog.timeout.connect(self._pump)

    def start(self) -> None:
        if not self._queue:
            self._finish()
            return
        if self._check_whitelist:
            self.debug.emit("-> fetching Wabbajack ServerWhitelist.yml")
            reply = self._manager.get(_request(WHITELIST_URL, 30))
            reply.finished.connect(lambda: self._on_whitelist(reply))
        else:
            self._begin()

    def stop(self) -> None:
        self._stopping = True
        self._queue.clear()
        for reply in list(self._inflight):
            try:
                reply.abort()
            except Exception:
                pass
        if not self._inflight:
            self._finish()

    def _begin(self) -> None:
        self._watchdog.start()
        self._pump()

    def _finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        self._watchdog.stop()
        self.finished.emit()

    def _on_whitelist(self, reply) -> None:
        try:
            raw = bytes(reply.readAll()).decode("utf-8", "replace")
        except Exception:
            raw = ""
        reply.deleteLater()
        prefixes = re.findall(r"^\s*-\s*(https?://\S+)\s*$", raw, re.MULTILINE)
        if prefixes:
            self._whitelist = [p.rstrip("/").lower() for p in prefixes]
            self.logged.emit(f"Loaded {len(self._whitelist)} whitelisted URL prefixes from Wabbajack.")
        else:
            self.logged.emit("Could not read the Wabbajack whitelist; skipping the whitelist check.")
        self._begin()

    def _whitelist_note(self, url: str) -> str:
        if not self._whitelist:
            return ""
        lowered = url.lower()
        if any(lowered.startswith(prefix) for prefix in self._whitelist):
            return ""
        if _host_of(url).endswith(("nexusmods.com", "github.com", "wabbajack.org")):
            return ""
        return " NOT on the Wabbajack server whitelist - compilation will reject it"

    def _pump(self) -> None:
        while not self._stopping and self._queue and len(self._inflight) < self._max_in_flight:
            self._send(self._queue.pop(0), use_get=False)
        if not self._queue and not self._inflight and not self._stopping:
            self._finish()

    def _send(self, index: int, use_get: bool) -> None:
        entry = self._entries[index]
        host = _host_of(entry.url)
        if host in OPAQUE_HOSTS and not use_get:
            self._resolve(index, ST_ERROR, f"Not checkable automatically - {OPAQUE_HOSTS[host]}. "
                          f"Open the link and confirm by hand{self._whitelist_note(entry.url)}")
            return
        if not entry.url.lower().startswith(("http://", "https://")) or not QUrl(entry.url).isValid():
            self._resolve(index, ST_MOD_GONE, f"'{entry.url}' is not a usable http(s) URL")
            return
        self.debug.emit(f"-> {'GET' if use_get else 'HEAD'} {entry.url}")
        request = _request(entry.url, self._timeout, ranged=use_get)
        reply = self._manager.get(request) if use_get else self._manager.head(request)
        self._inflight[reply] = (index, use_get)
        reply.finished.connect(lambda: self._on_reply(reply))

    def _on_reply(self, reply) -> None:
        record = self._inflight.pop(reply, None)
        if record is None:
            reply.deleteLater()
            return
        index, was_get = record
        entry = self._entries[index]
        code = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        content_type = str(reply.header(QNetworkRequest.KnownHeaders.ContentTypeHeader) or "")
        length = reply.header(QNetworkRequest.KnownHeaders.ContentLengthHeader)
        content_range = bytes(reply.rawHeader(b"Content-Range") or b"").decode("ascii", "replace")
        reply.deleteLater()
        self.debug.emit(f"<- {code} {entry.url} type={content_type!r} len={length!r} range={content_range!r}")
        if not was_get and code in (400, 403, 405, 501):
            self.debug.emit("   HEAD refused, retrying with ranged GET")
            self._send(index, use_get=True)
            return
        self._classify(index, code, content_type, length, content_range)
        self._pump()

    def _classify(self, index: int, code: Optional[int], content_type: str, length: Any,
                  content_range: str) -> None:
        entry = self._entries[index]
        host = _host_of(entry.url)
        note = self._whitelist_note(entry.url)
        remote_size: Optional[int] = None
        match = re.search(r"/(\d+)\s*$", content_range) if content_range else None
        if match:
            remote_size = int(match.group(1))
        elif isinstance(length, int) and length > 1:
            remote_size = length
        entry.remote_size = remote_size

        if code is None:
            return self._resolve(index, ST_ERROR, f"no HTTP response (host unreachable){note}")
        if code in (404, 410):
            return self._resolve(index, ST_MOD_GONE, f"HTTP {code} - the file is gone{note}")
        if code in (401, 403):
            return self._resolve(index, ST_ERROR, f"HTTP {code} - the host refused an anonymous check; verify by hand{note}")
        if code == 429:
            return self._resolve(index, ST_ERROR, f"HTTP 429 - rate limited by the host{note}")
        if code >= 500:
            return self._resolve(index, ST_ERROR, f"HTTP {code} - host error, try again later{note}")
        if code >= 400:
            return self._resolve(index, ST_MOD_GONE, f"HTTP {code}{note}")
        if host in OPAQUE_HOSTS:
            return self._resolve(index, ST_ERROR, f"Link responds, but {OPAQUE_HOSTS[host]} - check by hand{note}")
        if entry.kind == KIND_MANUAL:
            return self._resolve(index, ST_OK, f"Landing page reachable (HTTP {code}). manualURL contents "
                                 f"cannot be verified automatically - confirm the file is still on the page{note}")
        if host in SOFT_HOSTS:
            return self._resolve(index, ST_OK, f"Reachable (HTTP {code}), but {SOFT_HOSTS[host]}{note}")
        if content_type.lower().startswith(("text/html", "text/plain")):
            return self._resolve(index, ST_FILE_GONE, f"HTTP {code} but the server returned a page instead "
                                 f"of a file - usually a 'not found' page with a 200 status{note}")
        if entry.local_size and remote_size and remote_size != entry.local_size:
            return self._resolve(index, ST_FILE_GONE, f"File is served but its size changed: "
                                 f"{human_size(remote_size)} online vs {human_size(entry.local_size)} "
                                 f"locally - the author likely re-uploaded different content{note}")
        size_text = f", {human_size(remote_size)}" if remote_size else ""
        if note:
            return self._resolve(index, ST_FILE_GONE, f"Downloadable (HTTP {code}{size_text}) but{note}")
        self._resolve(index, ST_OK, f"Downloadable (HTTP {code}{size_text})")

    def _resolve(self, index: int, status: str, reason: str) -> None:
        entry = self._entries[index]
        entry.status, entry.reason = status, reason
        self._done += 1
        self.progressed.emit(self._done, self._total)
        self.entryResolved.emit([index])


# ---------------------------------------------------------------------------
# Fetching missing archives through MO2's download manager
# ---------------------------------------------------------------------------


class DownloadRunner(QObject):
    progressed = pyqtSignal(int, int)
    entryResolved = pyqtSignal(object)
    logged = pyqtSignal(str)
    debug = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self, organizer: mobase.IOrganizer, entries: List[Entry], indices: List[int],
                 max_active: int = 2, restore_meta: bool = True,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._entries = entries
        self._queue = list(indices)
        self._total = len(indices)
        self._done = 0
        self._max_active = max(1, max_active)
        self._restore_meta = restore_meta
        self._stopping = False
        self._downloads_path = organizer.downloadsPath()
        self._manager = organizer.downloadManager()
        if self._manager is None:
            raise RuntimeError("MO2 provided no download manager.")
        self._active: Dict[int, int] = {}
        self._poll = QTimer(self)
        self._poll.setInterval(4000)
        self._poll.timeout.connect(self._poll_disk)

    def start(self) -> None:
        if not self._queue:
            self.finished.emit()
            return
        self._poll.start()
        self._pump()

    def stop(self) -> None:
        self._stopping = True
        remaining = len(self._queue)
        self._queue.clear()
        if remaining:
            self.logged.emit(f"Stopped. {remaining} archive(s) were not queued; downloads already "
                             "handed to MO2 continue in its Downloads tab.")
        if not self._active:
            self._finish()

    def _finish(self) -> None:
        self._poll.stop()
        self.finished.emit()

    def _pump(self) -> None:
        while not self._stopping and self._queue and len(self._active) < self._max_active:
            self._launch(self._queue.pop(0))
        if not self._queue and not self._active:
            self._finish()

    def _launch(self, index: int) -> None:
        entry = self._entries[index]
        try:
            if entry.kind == KIND_NEXUS:
                self.debug.emit(f"-> startDownloadNexusFile(mod={entry.mod_id}, file={entry.file_id})")
                download_id = self._manager.startDownloadNexusFile(int(entry.mod_id), int(entry.file_id))
            else:
                self.debug.emit(f"-> startDownloadURLs([{entry.url}])")
                download_id = self._manager.startDownloadURLs([entry.url])
        except Exception as exc:
            self._settle(index, ST_DL_FAILED, f"MO2 refused the download: {exc}")
            return
        if download_id is None or int(download_id) < 0:
            self._settle(index, ST_DL_FAILED, "MO2 returned no download ID. For Nexus files this "
                         "usually means the account is not Premium - the download API is Premium-only.")
            return
        self._active[int(download_id)] = index
        entry.status = ST_DOWNLOADING
        entry.reason = f"Queued in MO2 (download #{int(download_id)})"
        self.entryResolved.emit([index])

    def handle_complete(self, download_id: int) -> None:
        index = self._active.pop(int(download_id), None)
        if index is None:
            return
        entry = self._entries[index]
        path = ""
        try:
            path = self._manager.downloadPath(int(download_id)) or ""
        except Exception:
            pass
        if path:
            entry.archive = os.path.basename(path)
        self._restore_meta_keys(entry, path)
        size = ""
        try:
            if path and os.path.isfile(path):
                size = f" ({human_size(os.path.getsize(path))})"
        except OSError:
            pass
        self._settle(index, ST_DOWNLOADED, f"Downloaded{size}", already_removed=True)
        self._pump()

    def handle_failed(self, download_id: int) -> None:
        index = self._active.pop(int(download_id), None)
        if index is None:
            return
        self._settle(index, ST_DL_FAILED, "MO2 reported the download as failed - check its "
                     "Downloads tab for the reason", already_removed=True)
        self._pump()

    def _poll_disk(self) -> None:
        for download_id, index in list(self._active.items()):
            entry = self._entries[index]
            if entry.archive and not archive_missing(entry, self._downloads_path):
                self._active.pop(download_id, None)
                self._settle(index, ST_DOWNLOADED, "Downloaded (seen on disk)", already_removed=True)
        self._pump()

    def _settle(self, index: int, status: str, reason: str, already_removed: bool = False) -> None:
        entry = self._entries[index]
        entry.status, entry.reason = status, reason
        if not already_removed:
            for did, idx in list(self._active.items()):
                if idx == index:
                    self._active.pop(did, None)
        self._done += 1
        self.progressed.emit(self._done, self._total)
        self.entryResolved.emit([index])

    def _restore_meta_keys(self, entry: Entry, path: str) -> None:
        if not self._restore_meta or entry.kind not in (KIND_DIRECT, KIND_MANUAL) or not entry.url:
            return
        meta_path = (path + ".meta") if path else ""
        if not meta_path or not os.path.isdir(os.path.dirname(meta_path)):
            return
        try:
            meta = QSettings(meta_path, QSettings.Format.IniFormat)
            key = "General/directURL" if entry.kind == KIND_DIRECT else "General/manualURL"
            if not meta.value(key):
                meta.setValue(key, entry.url)
                if entry.kind == KIND_MANUAL and entry.prompt:
                    meta.setValue("General/prompt", entry.prompt)
                meta.sync()
                self.debug.emit(f"   restored {key} in {os.path.basename(meta_path)}")
        except Exception as exc:
            self.debug.emit(f"!! could not restore meta keys: {exc}")


# ---------------------------------------------------------------------------
# Online preflight: repositories.json -> modlists.json -> links + status report
# ---------------------------------------------------------------------------


class HttpFetcher(QObject):
    """Small callback-based HTTP helper on Qt's network stack (uses Windows'
    certificate store, unlike urllib in MO2's embedded Python)."""

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._manager = QNetworkAccessManager(self)
        self._replies: Set[Any] = set()

    def get(self, url: str, callback: Callable[[Optional[int], Optional[str], str], None],
            timeout_s: int = 25, ranged: bool = False, headers: Optional[Dict[str, str]] = None) -> None:
        self._send("GET", url, callback, timeout_s, ranged, headers, None)

    def post_json(self, url: str, body: Dict[str, Any],
                  callback: Callable[[Optional[int], Optional[str], str], None],
                  headers: Optional[Dict[str, str]] = None, timeout_s: int = 60) -> None:
        import json as _json
        self._send("POST", url, callback, timeout_s, False, headers, _json.dumps(body).encode("utf-8"))

    def _send(self, method, url, callback, timeout_s, ranged, headers, data) -> None:
        if not url.lower().startswith(("http://", "https://")):
            callback(None, None, "")
            return
        request = _request(url, timeout_s, ranged=ranged)
        for key, value in (headers or {}).items():
            request.setRawHeader(key.encode("ascii"), value.encode("utf-8"))
        if data is not None:
            request.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/json")
            reply = self._manager.post(request, data)
        else:
            reply = self._manager.get(request)
        self._replies.add(reply)

        def done() -> None:
            self._replies.discard(reply)
            code = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
            ctype = str(reply.header(QNetworkRequest.KnownHeaders.ContentTypeHeader) or "")
            body = None if ranged else bytes(reply.readAll()).decode("utf-8-sig", "replace")
            reply.deleteLater()
            callback(int(code) if code is not None else None, body, ctype)

        reply.finished.connect(done)

    def download(self, url: str, path: str, progress: Callable[[int, int], None],
                 callback: Callable[[bool, str], None]) -> None:
        """Stream a (possibly large) file to disk."""
        request = _request(url, 600)
        reply = self._manager.get(request)
        self._replies.add(reply)
        tmp = path + ".part"
        try:
            handle = open(tmp, "wb")
        except OSError as exc:
            reply.abort()
            callback(False, str(exc))
            return

        def chunk() -> None:
            handle.write(bytes(reply.readAll()))

        def done() -> None:
            self._replies.discard(reply)
            handle.write(bytes(reply.readAll()))
            handle.close()
            code = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
            error = reply.errorString() if reply.error() != QNetworkReply.NetworkError.NoError else ""
            reply.deleteLater()
            if error or (code is not None and int(code) >= 400):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                callback(False, error or f"HTTP {code}")
                return
            os.replace(tmp, path)
            callback(True, path)

        reply.readyRead.connect(chunk)
        reply.downloadProgress.connect(lambda got, total: progress(int(got), int(total)))
        reply.finished.connect(done)

    def abort_all(self) -> None:
        for reply in list(self._replies):
            try:
                reply.abort()
            except Exception:
                pass


class OnlinePreflight(QObject):
    """Runs the network half of the preflight and emits a list of Findings."""

    progress = pyqtSignal(str)
    finished = pyqtSignal(object)

    def __init__(self, settings: Dict[str, Any], downloads: Set[str], parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._downloads = downloads
        self._http = HttpFetcher(self)
        self._findings: List[Finding] = []
        self._pending = 0
        self._entry: Optional[Dict[str, Any]] = None
        self._links_started = False
        self._done = False

    def start(self) -> None:
        repo, _machine = preflight.split_machine_url(self._settings)
        if repo:
            self.progress.emit("Fetching Wabbajack repositories.json...")
            self._http.get(preflight.REPOSITORIES_URL, self._on_repositories)
        else:
            self._findings.append(Finding(SEV_INFO, "Repository", "No MachineUrl, skipping repository checks"))
            self._start_links()

    def stop(self) -> None:
        self._http.abort_all()
        self._emit()

    def _on_repositories(self, code, text, _ctype) -> None:
        findings, modlists_url, repo_key = preflight.check_repositories(self._settings, text, code)
        self._findings += findings
        if not modlists_url:
            self._start_links()
            return
        self.progress.emit("Fetching your modlists.json...")
        self._http.get(modlists_url, lambda c, t, ct: self._on_modlists(c, t, modlists_url, repo_key))

    def _on_modlists(self, code, text, url, repo_key) -> None:
        output = str(self._settings.get("OutputFile") or "")
        free = free_bytes(os.path.dirname(output)) if output else None
        findings, entry = preflight.check_modlists_json(self._settings, text, code, url, repo_key, free)
        self._findings += findings
        self._entry = entry
        repo, machine = preflight.split_machine_url(self._settings)
        status_url = preflight.STATUS_URL.format(repo=repo_key or repo, machine=machine)
        self._pending += 1
        self._http.get(status_url, lambda c, t, ct: self._on_status(c, t, status_url))
        self._start_links()

    def _on_status(self, code, text, url) -> None:
        self._findings += preflight.check_status_report(text, code, url, self._downloads)
        self._settle()

    def _start_links(self) -> None:
        if self._links_started:
            return
        self._links_started = True
        targets = preflight.link_targets(self._settings, self._entry)
        self.progress.emit(f"Checking {len(targets)} link(s)...")
        for label, url in targets:
            probe = preflight.discord_probe_url(url)
            self._pending += 1
            self._http.get(probe or url,
                           lambda c, t, ct, l=label, u=url: self._on_link(l, u, c, ct),
                           ranged=not probe)
        if self._pending == 0:
            self._emit()

    def _on_link(self, label, url, code, ctype) -> None:
        self._findings.append(preflight.link_finding(label, url, code, ctype))
        self._settle()

    def _settle(self) -> None:
        self._pending -= 1
        if self._pending <= 0 and self._links_started:
            self._emit()

    def _emit(self) -> None:
        if self._done:
            return
        self._done = True
        self.finished.emit(self._findings)


# ---------------------------------------------------------------------------
# Collection build (runs off the UI thread)
# ---------------------------------------------------------------------------


class BuildThread(QThread):
    """Runs builder.run_build: extraction, FOMOD file mapping, copying,
    profile writing. Signals are delivered to the UI thread."""

    progressed = pyqtSignal(int, int, str)
    installed = pyqtSignal(object)
    done = pyqtSignal(object)

    def __init__(self, plan, extractor, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._plan = plan
        self._extractor = extractor
        self._stop = False

    def request_stop(self) -> None:
        self._stop = True

    def run(self) -> None:  # noqa: D401 - QThread override
        from .builder import run_build
        try:
            report = run_build(
                self._plan, self._extractor,
                progress=lambda i, t, n: self.progressed.emit(i, t, n),
                result_cb=lambda r: self.installed.emit(r),
                should_stop=lambda: self._stop,
            )
            self.done.emit(report)
        except Exception as exc:  # surfaced in the UI
            self.done.emit(exc)
