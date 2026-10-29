"""Telegram interface for Noor coding agent."""
from __future__ import annotations

import io
import logging
import os
import tempfile
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from project_builder import build_project

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("noor-bot")
MAX_IMAGE_BYTES = 8 * 1024 * 1024


def allowed_user(update: Update) -> bool:
    """Optional allowlist: set NOOR_ALLOWED_USER_IDS=123,456 for private use."""
    raw = os.getenv("NOOR_ALLOWED_USER_IDS", "").strip()
    # Fail closed. Configure your Telegram numeric user ID in Render before using the bot.
    if not raw:
        return False
    try:
        allowed = {int(x.strip()) for x in raw.split(",") if x.strip()}
    except ValueError:
        return False
    return bool(update.effective_user and update.effective_user.id in allowed)


async def reject_unallowed(update: Update) -> bool:
    if allowed_user(update):
        return False
    if update.effective_message:
        await update.effective_message.reply_text("هذا البوت غير مفعّل للاستخدام بعد. على مالك البوت ضبط NOOR_ALLOWED_USER_IDS في إعدادات Render.")
    return True


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await reject_unallowed(update):
        return
    await update.effective_message.reply_text(
        "مرحباً، أنا نور — وكيل بناء المشاريع البرمجية.\n\n"
        "أرسل وصف المشروع أو استخدم /build ثم وصفك. يمكنك إرفاق صورة مخطط تدفق مع وصفها في تعليق الصورة.\n"
        "سأولّد الملفات، وأجري فحوصات، وأحاول إصلاح فشل الاختبارات قبل إرسال ZIP.\n\n"
        "مهم: الفحوصات ليست ضماناً لانعدام الأخطاء، ولا تشغّل المشروع المستلم قبل مراجعة الكود."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await reject_unallowed(update):
        return
    await update.effective_message.reply_text(
        "طريقة الاستخدام:\n"
        "• أرسل: /build أنشئ تطبيق إدارة مهام بلغة Python مع اختبارات.\n"
        "• للمخطط: أرسل صورة المخطط مع تعليق يشرح المطلوب.\n"
        "• إذا فشلت الفحوصات بعد محاولات الإصلاح، لن أرسل ZIP على أنه جاهز؛ سأرسل تقرير الفشل.\n"
        "• حالياً فحص Python أقوى؛ اللغات الأخرى قد تخضع لفحص هيكلي فقط."
    )


async def get_image_bytes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bytes | None:
    msg = update.effective_message
    if not msg or not msg.photo:
        return None
    photo = msg.photo[-1]
    if photo.file_size and photo.file_size > MAX_IMAGE_BYTES:
        raise ValueError("حجم الصورة كبير. أرسل صورة أصغر من 8 ميغابايت.")
    telegram_file = await context.bot.get_file(photo.file_id)
    with tempfile.TemporaryDirectory(prefix="noor_img_") as tmp:
        path = Path(tmp) / "diagram.jpg"
        await telegram_file.download_to_drive(custom_path=str(path))
        data = path.read_bytes()
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("حجم الصورة بعد التنزيل أكبر من الحد المسموح.")
    return data


async def build_and_send(update: Update, context: ContextTypes.DEFAULT_TYPE, prompt: str, image_bytes: bytes | None = None) -> None:
    msg = update.effective_message
    if not msg:
        return
    if not prompt.strip():
        await msg.reply_text("اكتب وصف المشروع بعد /build، أو أرسل صورة المخطط مع وصف في تعليق الصورة.")
        return
    status = await msg.reply_text("بدأ نور تحليل المتطلبات وبناء المشروع. قد يستغرق ذلك عدة دقائق...")
    await context.bot.send_chat_action(chat_id=msg.chat_id, action=ChatAction.TYPING)
    try:
        result = await context.application.bot_data["build_semaphore"].run(prompt, image_bytes)
        if not result["ok"]:
            await status.edit_text(
                "لم أرسل المشروع لأن الفحوصات لم تنجح بعد محاولات الإصلاح.\n\n"
                f"المشروع: {result.get('name', 'غير محدد')}\n"
                f"التقرير:\n{result.get('report', 'لا يوجد تقرير')}\n\n"
                "أرسل طلباً لتعديل المتطلبات أو راجع التقرير ثم أعد المحاولة."
            )
            return
        summary = result.get("summary", "تم إنشاء المشروع.")[:1200]
        report = result.get("report", "")[:2500]
        await status.edit_text(
            f"اجتاز المشروع الفحوصات التي أمكن تشغيلها.\n"
            f"اسم المشروع: {result['name']}\n"
            f"محاولات البناء: {result['attempts']}\n\n"
            f"الملخص: {summary}\n\n"
            f"تقرير الاختبار:\n{report}\n\n"
            "ملاحظة: نجاح الفحوصات لا يضمن خلو المشروع من جميع الأخطاء."
        )
        archive = io.BytesIO(result["zip_bytes"])
        archive.name = f"{result['name']}.zip"
        await msg.reply_document(document=archive, filename=archive.name, caption="ملفات المشروع + README + الاختبارات المتاحة.")
    except Exception as exc:
        log.exception("Project build failed")
        safe_error = str(exc).replace(os.getenv("OPENAI_API_KEY", "__never__"), "[REDACTED]")[:1000]
        await status.edit_text("تعذر إكمال البناء.\nالسبب: " + safe_error)


class BuildSemaphore:
    """Keep the blocking SDK and test runner off Telegram's asyncio loop; allow one build at a time."""
    def __init__(self):
        import asyncio
        self._semaphore = asyncio.Semaphore(1)

    async def run(self, prompt: str, image_bytes: bytes | None):
        import asyncio
        async with self._semaphore:
            return await asyncio.to_thread(build_project, prompt, image_bytes)


async def build_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await reject_unallowed(update):
        return
    prompt = " ".join(context.args).strip()
    await build_and_send(update, context, prompt)


async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await reject_unallowed(update):
        return
    msg = update.effective_message
    if not msg or not msg.text:
        return
    await build_and_send(update, context, msg.text.strip())


async def photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await reject_unallowed(update):
        return
    msg = update.effective_message
    prompt = (msg.caption or "").strip() if msg else ""
    if not prompt:
        await msg.reply_text("أعد إرسال الصورة مع تعليق يشرح المشروع المطلوب والمخطط.")
        return
    try:
        image_bytes = await get_image_bytes(update, context)
        await build_and_send(update, context, prompt, image_bytes)
    except Exception as exc:
        await msg.reply_text("لم أستطع معالجة الصورة: " + str(exc)[:500])


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("ضع TELEGRAM_BOT_TOKEN في متغير البيئة قبل التشغيل.")
    if not os.getenv("OPENAI_API_KEY", "").strip():
        raise SystemExit("ضع OPENAI_API_KEY في متغير البيئة. بناء مشاريع عامة يحتاج نموذجاً لغوياً عبر API أو نموذجاً محلياً.")
    app = Application.builder().token(token).build()
    app.bot_data["build_semaphore"] = BuildSemaphore()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("build", build_command))
    app.add_handler(MessageHandler(filters.PHOTO, photo_message))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))
    log.info("Noor project-builder bot starting")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
