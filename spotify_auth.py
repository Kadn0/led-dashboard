#!/usr/bin/env python3
import os, sys, json, base64, requests
from pathlib import Path
from urllib.parse import urlencode

def get_credentials():
    print("Enter your Spotify API credentials:")
    client_id = input("Client ID: ").strip()
    client_secret = input("Client Secret: ").strip()
    if not client_id or not client_secret:
        print("Error: Both required")
        sys.exit(1)
    return client_id, client_secret

def get_auth_code(client_id):
    scope = "user-read-currently-playing"
    redirect_uri = "https://httpbin.org/get"
    auth_params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": scope
    }
    auth_url = f"https://accounts.spotify.com/authorize?{urlencode(auth_params)}"
    print(f"\n1. Open this URL: {auth_url}")
    print("2. Click 'Agree'")
    print("3. You'll be redirected to httpbin")
    print("4. Look at the URL bar - copy the 'code' parameter")
    print("   Example: https://httpbin.org/get?code=ABC123...")
    auth_code = input("\nPaste the code: ").strip()
    if not auth_code:
        print("Error: No code provided")
        sys.exit(1)
    return auth_code

def exchange_code_for_token(client_id, client_secret, auth_code):
    url = "https://accounts.spotify.com/api/token"
    redirect_uri = "https://httpbin.org/get"
    auth_str = f"{client_id}:{client_secret}"
    auth_bytes = auth_str.encode("utf-8")
    auth_base64 = base64.b64encode(auth_bytes).decode("utf-8")
    headers = {
        "Authorization": f"Basic {auth_base64}",
        "Content-Type": "application/x-www-form-urlencoded"
    }
    data = {
        "grant_type": "authorization_code",
        "code": auth_code,
        "redirect_uri": redirect_uri
    }
    try:
        response = requests.post(url, headers=headers, data=data, timeout=10)
        response.raise_for_status()
        return response.json().get("refresh_token")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

def save_config(client_id, client_secret, refresh_token):
    config_file = Path(os.path.expanduser("~/.spotify_display.conf"))
    config = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token
    }
    with open(config_file, "w") as f:
        json.dump(config, f, indent=2)
    os.chmod(config_file, 0o600)
    print(f"\n✓ Config saved!")
    print("Run: ~/spotify_env/bin/python3 ~/spotify_display.py")

if __name__ == "__main__":
    client_id, client_secret = get_credentials()
    auth_code = get_auth_code(client_id)
    refresh_token = exchange_code_for_token(client_id, client_secret, auth_code)
    if not refresh_token:
        print("Error: Could not obtain refresh token")
        sys.exit(1)
    save_config(client_id, client_secret, refresh_token)
