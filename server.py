import asyncio
from aiohttp import web
from pathlib import Path
import subprocess
import logging
import random
from datetime import datetime, timedelta

from mutagen.mp3 import MP3
from mutagen.id3 import ID3, ID3NoHeaderError

DEFAULT_BITRATE = 320
DEFAULT_CODEC = 'libmp3lame'
DEFAULT_FORMAT = 'mp3'
DEFAULT_CONTENT_TYPE = 'audio/mpeg'
ALBUM_TRACK_CHANCE = 0.1  # 10% chance for album tracks
SORT_INTERVAL_HOURS = 7  # Interval for reshuffling tracks

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
        # Check if we need to reshuffle
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

    def remove_apic_metadata(self, file_path):
        try:
            audio = MP3(file_path, ID3=ID3)
            has_apic = False

            apic_keys = [tag for tag in audio.tags.keys() if tag.startswith('APIC:')]

            for key in apic_keys:
                del audio.tags[key]
                has_apic = True

            if has_apic:
                audio.save()
                print(f"Removed APIC from {file_path}")
            else:
                print(f"No APIC metadata found in {file_path}")
        except Exception as e:
            print(f"Error processing file {file_path}: {e}")

    async def generate_audio(self):
        while True:
            track = self._next_track()
            self.current_track = track
            logging.info(f"Now playing: {track}")

            await self._play_track(track)

    async def _play_track(self, track):
        self.remove_apic_metadata(track)
        cmd = [
            self.ffmpeg, '-re', '-i', str(track),'-c:a', DEFAULT_CODEC,
            '-b:a', f'{self.bitrate}k', '-f', DEFAULT_FORMAT, '-map_metadata', '-1', 'pipe:1'
        ]
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        try:
            consecutive_empty_reads = 0
            max_empty_reads = 10

            while True:
                if process.returncode is not None:
                    logging.warning(
                        f"FFmpeg process for track {track} terminated unexpectedly with code {process.returncode}.")
                    break
                try:
                    chunk = await asyncio.wait_for(process.stdout.read(8192), timeout=5)
                    if chunk:
                        consecutive_empty_reads = 0
                        await self.broadcast(chunk)
                    else:
                        consecutive_empty_reads += 1
                        if consecutive_empty_reads >= max_empty_reads:
                            logging.warning(f"Too many consecutive empty reads for track {track}. Stopping playback.")
                            break
                except asyncio.TimeoutError:
                    logging.warning("Timeout while reading from FFmpeg process.")
                    consecutive_empty_reads += 1
                    if consecutive_empty_reads >= max_empty_reads:
                        break
        except Exception as e:
            logging.error(f"Error streaming audio: {e}")
        finally:
            if process.returncode is None:
                try:
                    process.terminate()
                    await process.wait()
                except ProcessLookupError:
                    logging.warning("Process already terminated.")
                except Exception as e:
                    logging.error(f"Error while terminating process: {e}")
            stderr = await process.stderr.read()
            if stderr:
                logging.debug(f"FFmpeg stderr output: {stderr.decode().strip()}")

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
    parser.add_argument('--host', type=str, default='127.0.0.1', help='Host to listen on')
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
