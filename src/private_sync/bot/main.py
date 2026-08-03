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
_PARTIAL_SEND_MESSAGE = (
    "전송이 {total}개 중 {sent}개 파트에서 끊겼습니다.\n"
    "이미 받은 파트는 지우고 처음부터 다시 받아주세요."
)
_DELIVERY_FAILED_MESSAGE = (
    "목록을 표시할 수 없습니다. 항목이 너무 많거나 일시적인 오류입니다.\n"
    "/find <키워드> 로 찾아보세요."
)


class Deliverer:
    """핸들러가 만든 Action을 실제 텔레그램 호출로 옮긴다."""

    def __init__(self, client: TelegramClient, config: BotConfig) -> None:
        self._client = client
        self._config = config

    def run(self, action: Action, incoming: Incoming) -> None:
        """Action을 실행한다. 실패는 로깅하고 사용자에게 알린다."""
        if incoming.callback_id:
            self.answer(incoming.callback_id)

        if action is None:
            return

        if isinstance(action, SendText):
            self._send_text(action, incoming)
            return

        self._send_file(action, incoming)

    def answer(self, callback_id: str) -> None:
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
            # 침묵하면 사용자에게는 버튼이 죽은 것으로 보인다. notify 는 자체적으로
            # TelegramError 를 삼키므로 여기서 다시 감싸지 않는다.
            self.notify(_DELIVERY_FAILED_MESSAGE)

    def notify(self, text: str) -> None:
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
            self.notify(_MISSING_FILE_MESSAGE)
            return

        workdir = Path(tempfile.mkdtemp(prefix="private-sync-"))
        sent = 0
        try:
            parts = pack_for_send(source, workdir, self._config.zip_password)
            for part in parts:
                self._client.send_document(
                    self._config.chat_id, part, caption=part.name
                )
                sent += 1

            if len(parts) > 1:
                archive = source.name + ".zip"
                self.notify(_SPLIT_NOTICE.format(count=len(parts), archive=archive))
            logger.info("Delivered %s as %d part(s)", action.rel, len(parts))
        except PackError as exc:
            logger.error("Packing failed for %s: %s", action.rel, exc)
            self.notify("파일을 포장하는 중 오류가 발생했습니다.")
        except (TelegramError, OSError) as exc:
            logger.error(
                "Sending failed for %s after %d part(s): %s",
                action.rel,
                sent,
                type(exc).__name__,
            )
            # 이미 보낸 파트가 있으면 재시도 시 같은 이름의 파트가 섞인다.
            # 사용자가 앞의 것을 버리도록 명시해야 결합 명령이 어긋나지 않는다.
            if sent:
                self.notify(_PARTIAL_SEND_MESSAGE.format(sent=sent, total=len(parts)))
            else:
                self.notify("파일 전송에 실패했습니다. 다시 시도해 주세요.")
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


def _report(deliverer: Deliverer, incoming: Incoming | None, text: str) -> None:
    """오류 안내를 보내고 콜백 로딩 표시도 함께 해제한다."""
    if incoming is not None and incoming.callback_id:
        deliverer.answer(incoming.callback_id)
    deliverer.notify(text)


def _handle_one(update: dict, context: Context, deliverer: Deliverer) -> None:
    """update 하나를 처리한다. 어떤 오류도 루프 밖으로 내보내지 않는다.

    여기서 예외가 새면 봇 프로세스가 죽는다. 텔레그램은 다음 getUpdates 가
    더 큰 offset 을 실어 보낼 때까지 같은 update 를 다시 보내므로, 재시작해도
    같은 자리에서 또 죽어 무한 크래시 루프가 된다.
    """
    incoming = None
    try:
        incoming = extract(update)
        if incoming is None:
            return

        action = handle(incoming, context)
        deliverer.run(action, incoming)
    except StoreError as exc:
        logger.warning("Store error while handling input: %s", exc)
        _report(deliverer, incoming, _MISSING_FILE_MESSAGE)
    except Exception as exc:  # noqa: BLE001
        # 심볼릭 루프는 RuntimeError 를, 기형 update 는 AttributeError·TypeError 를
        # 던진다. 타입을 좁게 나열하면 목록에 없는 것 하나로 봇이 내려간다.
        # 메시지 대신 타입명만 남기는 이유는 토큰이 담긴 URL 이 섞일 수 있어서다.
        # BLE001 을 끈 이유도 같다. ruff 가 blind except 를 봐주는 형태는
        # logger.exception 뿐인데, 그러면 토큰이 든 트레이스백이 로그에 남는다.
        logger.error("Unexpected error handling input: %s", type(exc).__name__)
        _report(deliverer, incoming, _INTERNAL_ERROR_MESSAGE)


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
            if not isinstance(update, dict):
                logger.warning("Skipping malformed update entry")
                continue

            raw_id = update.get("update_id")
            if isinstance(raw_id, int):
                offset = raw_id + 1
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
    # requests/urllib3 는 DEBUG 레벨에서 요청 URL 을 그대로 기록한다. 봇 API
    # URL 에는 토큰이 들어 있으므로 이 계층만 눌러 둔다.
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    try:
        config = load_bot_config(args.config)
    except ConfigError as exc:
        logger.error("Configuration error: %s", exc)
        return 1

    client = TelegramClient(config.token, api_base=config.api_base)
    try:
        client.get_me()
    except TelegramError as exc:
        # 로컬 API 서버를 쓰는 경우 그것이 죽어 있으면 봇이 통째로 먹통이 된다.
        # 조용히 도는 것보다 시작 시 분명히 멈추는 편이 낫다.
        logger.critical("Cannot reach the Telegram API at %s: %s", config.api_base, exc)
        return 1

    try:
        _serve(client, config)
    except KeyboardInterrupt:
        logger.info("Bot stopped by signal")
    return 0


if __name__ == "__main__":
    sys.exit(main())
