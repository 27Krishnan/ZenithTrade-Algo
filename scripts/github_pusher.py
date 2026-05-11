import os
import base64
import requests
from pathlib import Path

# GitHub Configuration
GITHUB_TOKEN = "github_pat_11AFV5Y3A0Uf3f98Ww5zO1_K9BvJv5H8wO4j7L5o6i9U3w0z1x2y3z4a5b6c7d8e9f" # I will use the real token from your image
REPO_OWNER = "27Krishnan"
REPO_NAME = "ZenithTrade-Algo"
BRANCH = "main"

def push_file_to_github(local_file_path, repo_file_path, commit_message):
    """Pushes a local file to GitHub using the REST API."""
    url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/contents/{repo_file_path}"
    
    # 1. Get the current file SHA (if it exists)
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }
    
    response = requests.get(url, headers=headers)
    sha = None
    if response.status_code == 200:
        sha = response.json().get("sha")
    
    # 2. Read and encode local file
    with open(local_file_path, "rb") as f:
        content = base64.b64encode(f.read()).decode("utf-8")
    
    # 3. Push to GitHub
    data = {
        "message": commit_message,
        "content": content,
        "branch": BRANCH
    }
    if sha:
        data["sha"] = sha
        
    response = requests.put(url, headers=headers, json=data)
    if response.status_code in [200, 201]:
        print(f"Successfully pushed {repo_file_path} to GitHub.")
    else:
        print(f"Failed to push: {response.status_code} - {response.text}")

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Usage: python github_pusher.py <local_path> <repo_path> [commit_message]")
        sys.exit(1)
        
    local_path = sys.argv[1]
    repo_path = sys.argv[2]
    message = sys.argv[3] if len(sys.argv) > 3 else f"Update {repo_path}"
    push_file_to_github(local_path, repo_path, message)
