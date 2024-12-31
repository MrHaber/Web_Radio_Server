# Web_Radio_Server
## Radio server for streaming mp3 tracks

Using
1. Upload all your mp3's to your folder
2. Prepare server libraries ```apt update && apt upgrade```
3. Install python 3.9 or newer
4. Install ffmpeg ```apt install ffmpeg``` or download manuality from [here](https://www.ffmpeg.org/)
5. After setting program with
```bash
python3 server.py --host 0.0.0.0 --port 8080 --music Music
```
Where:
```
--host -> server IP
--port -> listening port
--music -> folder where all music located
```
After all open your browser and go to ```host:port/echo```. Changing of source with config file will be added soon.
