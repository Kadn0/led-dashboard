#!/usr/bin/env python3
import os, sys, time, requests, base64, hashlib, json, subprocess
from io import BytesIO
from PIL import Image
from pathlib import Path

class SpotifyDisplay:
    def __init__(self, client_id, client_secret, refresh_token):
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.access_token = None
        self.token_expiry = 0
        self.current_album_url = None
        self.cache_dir = Path(os.path.expanduser("~/.spotify_display_cache"))
        self.cache_dir.mkdir(exist_ok=True)
    
    def refresh_access_token(self):
        url = "https://accounts.spotify.com/api/token"
        auth_str = f"{self.client_id}:{self.client_secret}"
        auth_base64 = base64.b64encode(auth_str.encode()).decode()
        headers = {"Authorization": f"Basic {auth_base64}", "Content-Type": "application/x-www-form-urlencoded"}
        data = {"grant_type": "refresh_token", "refresh_token": self.refresh_token}
        try:
            response = requests.post(url, headers=headers, data=data, timeout=5)
            response.raise_for_status()
            result = response.json()
            self.access_token = result["access_token"]
            self.token_expiry = time.time() + result.get("expires_in", 3600)
            return True
        except Exception as e:
            print(f"Token error: {e}")
            return False
    
    def ensure_valid_token(self):
        if time.time() >= self.token_expiry:
            return self.refresh_access_token()
        return True
    
    def get_currently_playing(self):
        if not self.ensure_valid_token():
            return None
        url = "https://api.spotify.com/v1/me/player/currently-playing"
        headers = {"Authorization": f"Bearer {self.access_token}"}
        try:
            response = requests.get(url, headers=headers, timeout=5)
            if response.status_code == 204:
                return None
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"Spotify error: {e}")
            return None
    
    def download_album_art(self, image_url):
        if not image_url:
            return None
        cache_key = hashlib.md5(image_url.encode()).hexdigest()
        cache_file = self.cache_dir / f"{cache_key}.jpg"
        if cache_file.exists():
            return str(cache_file)
        try:
            response = requests.get(image_url, timeout=5)
            response.raise_for_status()
            img = Image.open(BytesIO(response.content))
            img.save(cache_file, "JPEG")
            return str(cache_file)
        except Exception as e:
            print(f"Download error: {e}")
            return None
    
    def display_image(self, image_path):
        if not image_path:
            return
        try:
            subprocess.run(["bash", "/home/kadn/display_spotify_image.sh", image_path], timeout=5)
        except Exception as e:
            print(f"Display error: {e}")
    
    def run(self):
        print("Spotify Album Display started")
        try:
            while True:
                track_data = self.get_currently_playing()
                if track_data and "item" in track_data and track_data["item"]:
                    item = track_data["item"]
                    track_name = item.get("name", "Unknown")
                    artist_name = item["artists"][0]["name"] if item.get("artists") else "Unknown"
                    
                    if item.get("album", {}).get("images"):
                        image_url = item["album"]["images"][0]["url"]
                        
                        if image_url != self.current_album_url:
                            self.current_album_url = image_url
                            print(f"Now: {track_name} - {artist_name}")
                            image_path = self.download_album_art(image_url)
                            self.display_image(image_path)
                else:
                    if self.current_album_url is not None:
                        print("Nothing playing")
                        self.current_album_url = None
                
                time.sleep(2)
        except KeyboardInterrupt:
            print("\nStopped")

def main():
    config_file = Path(os.path.expanduser("~/.spotify_display.conf"))
    if not config_file.exists():
        print("Error: Config not found at ~/.spotify_display.conf")
        sys.exit(1)
    
    with open(config_file) as f:
        config = json.load(f)
    
    display = SpotifyDisplay(config["client_id"], config["client_secret"], config["refresh_token"])
    display.run()

if __name__ == "__main__":
    main()
