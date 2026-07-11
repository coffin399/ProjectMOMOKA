# MOMOKA/media_downloader/ytdlp_downloader_cog.py
import asyncio
import logging
import os
import uuid

import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from MOMOKA.media_downloader.error.errors import YTDLPExceptionHandler
from MOMOKA.music.plugins.ytdlp_wrapper import apply_youtube_ejs_opts

# --- 設定項目 ---
CLIENT_SECRETS_FILE = 'client_secrets.json'
TOKEN_FILE = 'token.json'
GDRIVE_FOLDER_ID = '1g5KmfB7xVrL-Y59RTf6f2IDbbJsTSFZs'  # ← ここを必ず書き換えてください
DELETE_DELAY_SECONDS = 600
DOWNLOAD_DIR = "temp_media_gdrive"
# --- 設定項目ここまで ---

logger = logging.getLogger(__name__)


class GDriveUploader:
    # (このクラスは変更ありません)
    def __init__(self, client_secrets_file, token_file):
        self.scopes = ['https://www.googleapis.com/auth/drive']
        self.client_secrets_file = client_secrets_file
        self.token_file = token_file
        self.service = self._get_drive_service()

    def _get_drive_service(self):
        creds = None
        if os.path.exists(self.token_file):
            creds = Credentials.from_authorized_user_file(self.token_file, self.scopes)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception as e:
                    logger.error(f"トークンのリフレッシュに失敗しました: {e}")
                    creds = None
            if not creds:
                logger.warning("-" * 60)
                logger.warning("Google Driveの認証が必要です。")
                logger.warning("コンソールに表示されるURLをブラウザで開き、アカウントを認証してください。")
                logger.warning("-" * 60)
                try:
                    flow = InstalledAppFlow.from_client_secrets_file(self.client_secrets_file, self.scopes)
                    creds = flow.run_local_server(port=0)
                except FileNotFoundError:
                    logger.critical(f"エラー: クライアントシークレットファイル '{self.client_secrets_file}' が見つかりません。")
                    return None
            with open(self.token_file, 'w') as token:
                token.write(creds.to_json())
        try:
            return build('drive', 'v3', credentials=creds)
        except Exception as e:
            logger.error(f"Google Driveサービスのビルド中にエラー: {e}")
            return None

    def upload_file(self, file_path, file_name, folder_id):
        if not self.service: return None, None
        file_metadata = {'name': file_name, 'parents': [folder_id]}
        media = MediaFileUpload(file_path, resumable=True)
        file = self.service.files().create(body=file_metadata, media_body=media, fields='id').execute()
        file_id = file.get('id')
        self.service.permissions().create(fileId=file_id, body={'type': 'anyone', 'role': 'reader'}).execute()
        download_link = f"https://drive.google.com/uc?export=download&id={file_id}"
        return file_id, download_link

    def delete_file(self, file_id):
        if not self.service: return
        try:
            self.service.files().delete(fileId=file_id).execute()
            logger.info(f"Google Drive上のファイルを削除しました: {file_id}")
        except HttpError as e:
            if e.resp.status == 404:
                logger.warning(f"削除しようとしたファイルが見つかりませんでした: {file_id}")
            else:
                logger.error(f"Google Drive上のファイル削除中にエラーが発生しました: {e}")
        except Exception as e:
            logger.error(f"Google Drive上のファイル削除中に予期せぬエラーが発生しました: {e}")


class VideoFormatSelect(discord.ui.Select):
    def __init__(self, cog_instance, info, url):
        self.cog = cog_instance
        self.info = info
        self.url = url
        options = []
        sorted_formats = sorted(
            [f for f in info.get('formats', []) if f.get('vcodec') != 'none'],
            key=lambda f: (f.get('height', 0), f.get('tbr', 0)),
            reverse=True
        )
        for f in sorted_formats[:25]:
            filesize = f.get('filesize') or f.get('filesize_approx')
            filesize_mb = f"{filesize / (1024 * 1024):.2f}MB" if filesize else "N/A"
            audio_note = " (映像のみ / Video Only)" if f.get('acodec') == 'none' else ""
            label = f"{f.get('resolution', 'N/A')}{audio_note} ({f.get('ext')}) - {filesize_mb}"
            description = f"Video: {f.get('vcodec', 'n/a')}, Audio: {f.get('acodec', 'n/a')}"
            options.append(discord.SelectOption(label=label, value=f.get('format_id'), description=description[:100]))
        super().__init__(
            placeholder="ダウンロードする動画フォーマットを選択してください / Select a video format to download...",
            min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            content=f"**{interaction.user.display_name}** がフォーマットを選択しました。\n**{interaction.user.display_name}** has selected a format.\n\n"
                    f"📥 ダウンロードと結合を開始します...\nStarting download and merge...",
            view=None, embed=None
        )
        format_id = self.values[0]
        video_title = self.info.get('title', 'video')
        base_uuid = str(uuid.uuid4())
        ydl_opts = apply_youtube_ejs_opts({
            'format': f"{format_id}+bestaudio[acodec^=mp4a]/bestvideo+bestaudio",
            'outtmpl': os.path.join(DOWNLOAD_DIR, f"{base_uuid}.%(ext)s"),
            'merge_output_format': 'mp4',
            'quiet': True,
            'no_warnings': True,
            'noprogress': True,
        })
        downloaded_file_path = None
        try:
            def download_sync():
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(self.url, download=False)
                    final_path = ydl.prepare_filename(info).rsplit('.', 1)[0] + '.mp4'
                    ydl.download([self.url])
                    return final_path if os.path.exists(final_path) else None

            downloaded_file_path = await asyncio.to_thread(download_sync)

            if not downloaded_file_path:
                # --- 変更: エラーハンドラを使用 ---
                await interaction.edit_original_response(content=self.cog.exception_handler.get_merge_error())
                return

            await interaction.edit_original_response(
                content=f"🔼 **{video_title}** をGoogle Driveにアップロードしています...\nUploading **{video_title}** to Google Drive...")
            upload_filename = f"{video_title}.mp4"
            file_id, download_link = await asyncio.to_thread(
                self.cog.gdrive_uploader.upload_file, downloaded_file_path, upload_filename, GDRIVE_FOLDER_ID
            )
            if not download_link:
                # --- 変更: エラーハンドラを使用 ---
                await interaction.edit_original_response(content=self.cog.exception_handler.get_upload_error())
                return

            minutes = int(DELETE_DELAY_SECONDS / 60)
            embed = discord.Embed(
                title="ダウンロード準備完了 / Download Ready",
                description=f"**{video_title}**\n\n"
                            f"以下のリンクからダウンロードしてください。\nPlease download from the link below.\n\n"
                            f"このリンクは**約{minutes}分後**に無効になります。\nThis link will expire in **about {minutes} minutes**.",
                color=discord.Color.green()
            )
            thumbnail_url = self.info.get('thumbnail')
            if thumbnail_url:
                embed.set_image(url=thumbnail_url)
            embed.add_field(name="ダウンロードリンク / Download Link",
                            value=f"[ここをクリック / Click Here]({download_link})", inline=False)

            await interaction.edit_original_response(content=None, embed=embed)
            asyncio.create_task(self.cog.schedule_gdrive_deletion(file_id))
        except Exception as e:
            # --- 変更: エラーハンドラを使用 ---
            await interaction.edit_original_response(content=self.cog.exception_handler.handle_exception(e))
        finally:
            logger.debug("[DEBUG] Cleaning up temporary files...")
            for item in os.listdir(DOWNLOAD_DIR):
                if item.startswith(base_uuid):
                    try:
                        os.remove(os.path.join(DOWNLOAD_DIR, item))
                    except OSError:
                        pass


class YtdlpGdriveCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.gdrive_uploader = GDriveUploader(CLIENT_SECRETS_FILE, TOKEN_FILE)
        # --- 追加: エラーハンドラインスタンスの作成 ---
        self.exception_handler = YTDLPExceptionHandler()
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    async def schedule_gdrive_deletion(self, file_id: str):
        await asyncio.sleep(DELETE_DELAY_SECONDS)
        await asyncio.to_thread(self.gdrive_uploader.delete_file, file_id)

    @app_commands.command(name="ytdlp_audio",
                          description="音声をダウンロードし、Google Drive経由で共有します。/ Downloads audio and shares it via Google Drive.")
    @app_commands.describe(
        query="YouTubeのURLまたは検索キーワード / YouTube URL or search query",
        audio_format="出力する音声フォーマット / Output audio format"
    )
    @app_commands.choices(audio_format=[
        app_commands.Choice(name="MP3", value="mp3"), app_commands.Choice(name="M4A", value="m4a"),
        app_commands.Choice(name="Opus", value="opus"), app_commands.Choice(name="FLAC", value="flac"),
        app_commands.Choice(name="WAV", value="wav"),
    ])
    async def ytdlp_audio(self, interaction: discord.Interaction, query: str, audio_format: str):
        if not self.gdrive_uploader.service:
            # --- 変更: エラーハンドラを使用 ---
            await interaction.response.send_message(self.exception_handler.get_gdrive_init_error())
            return
        await interaction.response.defer(thinking=True)
        unique_id = uuid.uuid4()
        output_path = os.path.join(DOWNLOAD_DIR, f"{unique_id}.{audio_format}")
        ydl_opts = apply_youtube_ejs_opts({
            'format': 'bestaudio*/best*',
            'outtmpl': os.path.join(DOWNLOAD_DIR, f"{unique_id}.%(ext)s"),
            'postprocessors': [
                {'key': 'FFmpegExtractAudio', 'preferredcodec': audio_format, 'preferredquality': '192'}],
            'noplaylist': True, 'default_search': 'ytsearch', 'quiet': True, 'no_warnings': True,
            'noprogress': True,
        })

        temp_original_file_path = None
        message = None
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = await asyncio.to_thread(ydl.extract_info, query, download=False)
                if 'entries' in info: info = info['entries'][0]
                video_title = info.get('title', 'audio')
                message = await interaction.followup.send(
                    f"📥 **{video_title}** をダウンロード・変換しています...\nDownloading & converting **{video_title}**...")
                temp_original_file_path = ydl.prepare_filename(info)
                await asyncio.to_thread(ydl.download, [query])
            if not os.path.exists(output_path):
                # --- 変更: エラーハンドラを使用 ---
                await message.edit(content=self.exception_handler.get_conversion_error())
                return
            await message.edit(
                content=f"🔼 **{video_title}** をGoogle Driveにアップロードしています...\nUploading **{video_title}** to Google Drive...")
            upload_filename = f"{video_title}.{audio_format}"
            file_id, download_link = await asyncio.to_thread(
                self.gdrive_uploader.upload_file, output_path, upload_filename, GDRIVE_FOLDER_ID
            )
            if not download_link:
                # --- 変更: エラーハンドラを使用 ---
                await message.edit(content=self.exception_handler.get_upload_error())
                return

            minutes = int(DELETE_DELAY_SECONDS / 60)
            embed = discord.Embed(
                title="ダウンロード準備完了 / Download Ready",
                description=f"**{video_title}**\n\n"
                            f"以下のリンクからダウンロードしてください。\nPlease download from the link below.\n\n"
                            f"このリンクは**約{minutes}分後**に無効になります。\nThis link will expire in **about {minutes} minutes**.",
                color=discord.Color.green()
            )
            embed.add_field(name="ダウンロードリンク / Download Link",
                            value=f"[ここをクリック / Click Here]({download_link})", inline=False)
            await message.edit(content=None, embed=embed)
            asyncio.create_task(self.schedule_gdrive_deletion(file_id))
        except Exception as e:
            # --- 変更: エラーハンドラを使用 ---
            error_msg = self.exception_handler.handle_exception(e)
            if message:
                await message.edit(content=error_msg)
            else:
                await interaction.followup.send(error_msg)
        finally:
            if os.path.exists(output_path): os.remove(output_path)
            if temp_original_file_path and os.path.exists(temp_original_file_path): os.remove(temp_original_file_path)

    @app_commands.command(name="ytdlp_video",
                          description="動画をダウンロードし、Google Drive経由で共有します。/ Downloads a video and shares it via Google Drive.")
    @app_commands.describe(query="ダウンロードしたい動画のURLまたは検索キーワード / URL or search query of the video")
    async def ytdlp_video(self, interaction: discord.Interaction, query: str):
        if not self.gdrive_uploader.service:
            # --- 変更: エラーハンドラを使用 ---
            await interaction.response.send_message(self.exception_handler.get_gdrive_init_error())
            return
        await interaction.response.defer(thinking=True)
        try:
            ydl_opts = apply_youtube_ejs_opts(
                {'quiet': True, 'default_search': 'ytsearch', 'noplaylist': True, 'noprogress': True}
            )

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = await asyncio.to_thread(ydl.extract_info, query, download=False)
                if 'entries' in info and info['entries']:
                    info = info['entries'][0]
            video_title = info.get('title', '不明なタイトル')
            video_url = info.get('webpage_url', query)
            thumbnail_url = info.get('thumbnail')
            uploader = info.get('uploader', 'N/A')
            duration = info.get('duration', 0)
            duration_str = "N/A"
            if duration:
                minutes, seconds = divmod(duration, 60)
                hours, minutes = divmod(minutes, 60)
                duration_str = (f"{hours:02}:" if hours > 0 else "") + f"{minutes:02}:{seconds:02}"

            embed = discord.Embed(
                title=video_title,
                url=video_url,
                description="ダウンロードしたい動画のフォーマットを選択してください:\nPlease select a video format to download:",
                color=discord.Color.red()
            )
            if thumbnail_url:
                embed.set_image(url=thumbnail_url)
            embed.set_footer(text=f"チャンネル / Channel: {uploader} | 再生時間 / Duration: {duration_str}")
            view = discord.ui.View(timeout=300)
            view.add_item(VideoFormatSelect(self, info, video_url))
            await interaction.followup.send(embed=embed, view=view)
        except Exception as e:
            # --- 変更: エラーハンドラを使用 ---
            await interaction.followup.send(self.exception_handler.handle_exception(e))


async def setup(bot: commands.Bot):
    await bot.add_cog(YtdlpGdriveCog(bot))