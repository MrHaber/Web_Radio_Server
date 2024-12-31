import asyncio
from aiohttp import web
from pathlib import Path
import subprocess
import logging
import random
from datetime import datetime, timedelta

DEFAULT_BITRATE = 128
DEFAULT_CODEC = 'libmp3lame'
DEFAULT_FORMAT = 'mp3'
DEFAULT_CONTENT_TYPE = 'audio/mpeg'
ALBUM_TRACK_CHANCE = 0.1
SORT_INTERVAL_HOURS = 7
ECHORYTHMS_TRACK = "echorythms.mp3"
FADE_IN_DURATION = 5

class RadioStream:
    def __init__(self, music_path: Path, ffmpeg: str, bitrate: int = DEFAULT_BITRATE):
        self.music_path = music_path
        self.ffmpeg = ffmpeg
        self.bitrate = bitrate
        self.clients = set()
        self.lock = asyncio.Lock()
        self.current_track = None
        self.tracks = []
        self.album_tracks = []
        self.last_shuffle_time = datetime.now()

        self._load_tracks()

    def _load_tracks(self):
        self.tracks = list(self.music_path.glob('*.mp3'))
        album_folders = [folder for folder in self.music_path.iterdir() if folder.is_dir()]
        self.album_tracks = [track for folder in album_folders for track in folder.glob('*.mp3')]
        logging.info(f"Loaded {len(self.tracks)} main tracks and {len(self.album_tracks)} album tracks.")
        self._shuffle_tracks()

    def _shuffle_tracks(self):
        random.shuffle(self.tracks)
        random.shuffle(self.album_tracks)

    def _next_track(self):
        if datetime.now() - self.last_shuffle_time >= timedelta(hours=SORT_INTERVAL_HOURS):
            self._shuffle_tracks()
            self.last_shuffle_time = datetime.now()

        if random.random() < ALBUM_TRACK_CHANCE and self.album_tracks:
            return random.choice(self.album_tracks)
        elif self.tracks:
            return random.choice(self.tracks)
        else:
            self._load_tracks()
            return self._next_track()

    async def generate_audio(self):
        while True:
            track = self._next_track()
            self.current_track = track
            logging.info(f"Now playing: {track}")

            await self._play_track(track)

            # Play echorythms.mp3 after each track
            echorythms_path = self.music_path / ECHORYTHMS_TRACK
            if echorythms_path.exists():
                logging.info(f"Now playing: {ECHORYTHMS_TRACK}")
                await self._play_track(echorythms_path)

    async def _play_track(self, track):
        cmd = [
            self.ffmpeg, '-re', '-i', str(track), '-af', f"afade=t=in:ss=0:d={FADE_IN_DURATION}", '-c:a', DEFAULT_CODEC,
            '-b:a', f'{self.bitrate}k', '-f', DEFAULT_FORMAT, '-map_metadata', '-1', 'pipe:1'
        ]
        process = await asyncio.create_subprocess_exec(*cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

        try:
            while True:
                chunk = await process.stdout.read(4096)
                if not chunk:
                    break
                await self.broadcast(chunk)
        except Exception as e:
            logging.error(f"Error streaming audio: {e}")
        finally:
            await process.wait()

    async def broadcast(self, chunk):
        async with self.lock:
            to_remove = []
            for client in self.clients:
                try:
                    await client.write(chunk)
                except (ConnectionResetError, asyncio.CancelledError):
                    logging.info("Client disconnected.")
                    to_remove.append(client)
                except Exception as e:
                    logging.error(f"Unexpected client error: {e}")
                    to_remove.append(client)
            for client in to_remove:
                self.clients.discard(client)

    async def register_client(self, client):
        async with self.lock:
            self.clients.add(client)

    async def unregister_client(self, client):
        async with self.lock:
            self.clients.discard(client)


async def stream_handler(request):
    response = web.StreamResponse()
    response.headers['Content-Type'] = DEFAULT_CONTENT_TYPE
    response.headers['Connection'] = 'keep-alive'
    response.enable_chunked_encoding()
    await response.prepare(request)

    await request.app['radio'].register_client(response)
    try:
        while True:
            await asyncio.sleep(1)  # Keep the connection open
    except asyncio.CancelledError:
        pass
    finally:
        await request.app['radio'].unregister_client(response)
    return response


async def start_radio(app):
    asyncio.create_task(app['radio'].generate_audio())


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler("radio_server.log"),
            logging.StreamHandler()
        ]
    )

    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', type=str, default='0.0.0.0', help='Host to listen on')
    parser.add_argument('--port', type=int, required=True, help='Port to listen on')
    parser.add_argument('--music', type=str, required=True, help='Path to the music directory')
    parser.add_argument('--ffmpeg', type=str, default='ffmpeg', help='Path to ffmpeg binary')
    parser.add_argument('--bitrate', type=int, default=DEFAULT_BITRATE, help='Bitrate in kbps')
    args = parser.parse_args()

    music_path = Path(args.music)
    if not music_path.is_dir():
        raise NotADirectoryError(f"Provided music path {music_path} is not a directory")

    app = web.Application()
    app['radio'] = RadioStream(music_path, args.ffmpeg, args.bitrate)
    app.router.add_get('/echo', stream_handler)

    app.on_startup.append(start_radio)

    web.run_app(app, host=args.host, port=args.port)
