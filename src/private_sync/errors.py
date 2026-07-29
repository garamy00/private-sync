"""private-sync 도메인 예외 계층."""


class PrivateSyncError(Exception):
    """모든 private-sync 예외의 기반."""


class ConfigError(PrivateSyncError):
    """설정 파일 또는 환경변수가 잘못됐다."""


class UploadError(PrivateSyncError):
    """업로드가 실패했고 재시도해도 소용없다."""


class RetryableUploadError(UploadError):
    """연결 실패 등 일시적 원인으로 업로드가 실패했다."""


class PackError(PrivateSyncError):
    """전송용 ZIP 생성에 실패했다."""


class StoreError(PrivateSyncError):
    """저장소 경로 접근이 거부됐거나 대상이 없다."""


class TelegramError(PrivateSyncError):
    """텔레그램 Bot API 호출이 실패했다."""
