from enum import Enum, auto


class MountState(Enum):
    DISCONNECTED = auto()
    MOUNTING = auto()
    MOUNTED = auto()
    UNMOUNTING = auto()
    ERROR = auto()
