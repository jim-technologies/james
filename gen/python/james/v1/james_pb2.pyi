from google.api import annotations_pb2 as _annotations_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class ListSessionsRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class SessionInfo(_message.Message):
    __slots__ = ("backend", "conversation_id")
    BACKEND_FIELD_NUMBER: _ClassVar[int]
    CONVERSATION_ID_FIELD_NUMBER: _ClassVar[int]
    backend: str
    conversation_id: str
    def __init__(self, backend: _Optional[str] = ..., conversation_id: _Optional[str] = ...) -> None: ...

class ListSessionsResponse(_message.Message):
    __slots__ = ("sessions",)
    SESSIONS_FIELD_NUMBER: _ClassVar[int]
    sessions: _containers.RepeatedCompositeFieldContainer[SessionInfo]
    def __init__(self, sessions: _Optional[_Iterable[_Union[SessionInfo, _Mapping]]] = ...) -> None: ...

class DispatchRequest(_message.Message):
    __slots__ = ("backend", "prompt", "channel", "conversation_id")
    BACKEND_FIELD_NUMBER: _ClassVar[int]
    PROMPT_FIELD_NUMBER: _ClassVar[int]
    CHANNEL_FIELD_NUMBER: _ClassVar[int]
    CONVERSATION_ID_FIELD_NUMBER: _ClassVar[int]
    backend: str
    prompt: str
    channel: str
    conversation_id: str
    def __init__(self, backend: _Optional[str] = ..., prompt: _Optional[str] = ..., channel: _Optional[str] = ..., conversation_id: _Optional[str] = ...) -> None: ...

class DispatchResponse(_message.Message):
    __slots__ = ("backend", "ok", "text", "error", "artifacts")
    BACKEND_FIELD_NUMBER: _ClassVar[int]
    OK_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    ARTIFACTS_FIELD_NUMBER: _ClassVar[int]
    backend: str
    ok: bool
    text: str
    error: str
    artifacts: _containers.RepeatedCompositeFieldContainer[Artifact]
    def __init__(self, backend: _Optional[str] = ..., ok: _Optional[bool] = ..., text: _Optional[str] = ..., error: _Optional[str] = ..., artifacts: _Optional[_Iterable[_Union[Artifact, _Mapping]]] = ...) -> None: ...

class Artifact(_message.Message):
    __slots__ = ("content", "mime", "filename")
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    MIME_FIELD_NUMBER: _ClassVar[int]
    FILENAME_FIELD_NUMBER: _ClassVar[int]
    content: bytes
    mime: str
    filename: str
    def __init__(self, content: _Optional[bytes] = ..., mime: _Optional[str] = ..., filename: _Optional[str] = ...) -> None: ...
