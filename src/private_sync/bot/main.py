"""서버 측 봇 진입점. 롱폴링으로 명령을 받아 파일을 전달한다."""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
import tempfile
import time
from pathlib import Path

from private_sync.bot import store
from private_sync.bot.handlers import (
    Action,
    Context,
    Incoming,
    SendFile,
    SendText,
    TokenMap,
    extract,
    handle,
)
from private_sync.bot.packer import pack_for_send
from private_sync.bot.telegram import TelegramClient
from private_sync.config import BotConfig, load_bot_config
from private_sync.errors import (
    ConfigError,
    PackError,
    PrivateSyncError,
    StoreError,
    TelegramError,
)

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG = Path("~/.config/private-sync/bot.yaml").expanduser()
_ERROR_SLEEP_SEC = 3
_MISSING_FILE_MESSAGE = "파일을 찾을 수 없습니다. 동기화 대기 중이거나 삭제되었습니다."
_INTERNAL_ERROR_MESSAGE = (
    "요청을 처리하는 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."
)
_SPLIT_NOTICE = (
    "파일이 커서 {count}개로 나눠 보냈습니다.\n"
    "PC에서 아래 명령으로 합친 뒤 비밀번호를 입력해 열어주세요.\n"
    "cat {archive}.part* > {archive}"
)


class Deliverer:
    """핸들러가 만든 Action을 실제 텔레그램 호출로 옮긴다."""

    def __init__(self, client: TelegramClient, config: BotConfig) -> None:
        self._client = client
        self._config = config

    def run(self, action: Action, incoming: Incoming) -> None:
        """Action을 실행한다. 실패는 로깅하고 사용자에게 알린다."""
        if incoming.callback_id:
            self._answer(incoming.callback_id)

        if action is None:
            return

        if isinstance(action, SendText):
            self._send_text(action, incoming)
            return

        self._send_file(action, incoming)

    def _answer(self, callback_id: str) -> None:
        """버튼 로딩 표시를 해제한다. 실패는 무시해도 무해하다."""
        try:
            self._client.answer_callback(callback_id)
        except TelegramError as exc:
            logger.warning("answerCallbackQuery failed: %s", exc)

    def _send_text(self, action: SendText, incoming: Incoming) -> None:
        try:
            if action.edit and incoming.message_id is not None:
                self._client.edit_message_text(
                    self._config.chat_id,
                    incoming.message_id,
                    action.text,
                    action.buttons,
                )
                return
            self._client.send_message(self._config.chat_id, action.text, action.buttons)
        except TelegramError as exc:
            logger.error("Failed to deliver text response: %s", exc)

    def _notify(self, text: str) -> None:
        """사용자에게 짧은 안내를 보낸다."""
        try:
            self._client.send_message(self._config.chat_id, text)
        except TelegramError as exc:
            logger.error("Failed to deliver notice: %s", exc)

    def _send_file(self, action: SendFile, _incoming: Incoming) -> None:
        """저장소 파일을 암호 ZIP으로 포장해 보낸다."""
        try:
            source = store.resolve_safe(self._config.store, action.rel)
        except StoreError as exc:
            logger.warning("Rejected file request %r: %s", action.rel, exc)
            self._notify(_MISSING_FILE_MESSAGE)
            return

        workdir = Path(tempfile.mkdtemp(prefix="private-sync-"))
        try:
            parts = pack_for_send(source, workdir, self._config.zip_password)
            for part in parts:
                self._client.send_document(
                    self._config.chat_id, part, caption=part.name
                )
            if len(parts) > 1:
                archive = source.name + ".zip"
                self._notify(_SPLIT_NOTICE.format(count=len(parts), archive=archive))
            logger.info("Delivered %s as %d part(s)", action.rel, len(parts))
        except PackError as exc:
            logger.error("Packing failed for %s: %s", action.rel, exc)
            self._notify("파일을 포장하는 중 오류가 발생했습니다.")
        except TelegramError as exc:
            logger.error("Sending failed for %s: %s", action.rel, exc)
            self._notify("파일 전송에 실패했습니다. 다시 시도해 주세요.")
        finally:
            # 평문·암호문 임시 파일을 남기지 않는다
            shutil.rmtree(workdir, ignore_errors=True)


def _build_context(config: BotConfig, tokens: TokenMap) -> Context:
    """저장소 조회를 주입한 핸들러 컨텍스트를 만든다."""
    return Context(
        chat_id=config.chat_id,
        tokens=tokens,
        lister=lambda rel: store.list_dir(config.store, rel),
        searcher=lambda keyword: store.search(config.store, keyword),
    )


def _handle_one(update: dict, context: Context, deliverer: Deliverer) -> None:
    """update 하나를 처리한다. 어떤 오류도 루프 밖으로 내보내지 않는다."""
    incoming = extract(update)
    if incoming is None:
        return

    try:
        action = handle(incoming, context)
    except StoreError as exc:
        logger.warning("Store error while handling input: %s", exc)
        deliverer.run(SendText(text=_MISSING_FILE_MESSAGE), incoming)
        return
    except (PrivateSyncError, OSError) as exc:
        # 깨진 심볼릭 링크 같은 예상 밖 오류로 봇 프로세스가 죽으면,
        # 사용자는 외부에서 자료를 꺼낼 수단을 통째로 잃는다.
        logger.error("Unexpected error handling input: %s", type(exc).__name__)
        deliverer.run(SendText(text=_INTERNAL_ERROR_MESSAGE), incoming)
        return

    deliverer.run(action, incoming)


def _serve(client: TelegramClient, config: BotConfig) -> None:
    """롱폴링 루프. 예외로 죽지 않는다."""
    tokens = TokenMap()
    context = _build_context(config, tokens)
    deliverer = Deliverer(client, config)
    offset: int | None = None

    logger.info("Bot started, serving store %s", config.store)
    while True:
        try:
            updates = client.get_updates(offset)
        except TelegramError as exc:
            logger.warning("getUpdates failed: %s", exc)
            time.sleep(_ERROR_SLEEP_SEC)
            continue

        for update in updates:
            offset = int(update.get("update_id", 0)) + 1
            _handle_one(update, context, deliverer)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="private-sync 텔레그램 봇")
    parser.add_argument("--config", type=Path, default=_DEFAULT_CONFIG)
    parser.add_argument("--debug", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """봇을 실행한다."""
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        config = load_bot_config(args.config)
    except ConfigError as exc:
        logger.error("Configuration error: %s", exc)
        return 1

    client = TelegramClient(config.token)
    try:
        _serve(client, config)
    except KeyboardInterrupt:
        logger.info("Bot stopped by signal")
    return 0


if __name__ == "__main__":
    sys.exit(main())
