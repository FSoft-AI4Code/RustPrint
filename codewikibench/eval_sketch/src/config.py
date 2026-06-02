import os
from pathlib import Path
from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv())

PROJECT_ROOT = Path(__file__).parent.parent.absolute()

# Common data paths relative to project root
DATA_DIR = PROJECT_ROOT / "data"
SRC_DIR = PROJECT_ROOT / "src"

API_KEY = os.getenv("API_KEY", "your-api-key-here")
MODEL = os.getenv("MODEL", "claude-sonnet-4")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "gemini-embedding-001")
BASE_URL = os.getenv("BASE_URL", "https://api.openai.com/v1/")

def get_project_path(*paths):
    """Get a path relative to the project root"""
    return str(PROJECT_ROOT.joinpath(*paths))

def get_data_path(*paths):
    """Get a path relative to the data directory"""
    return str(DATA_DIR.joinpath(*paths))

# max tokens per tool response
MAX_TOKENS_PER_TOOL_RESPONSE = 36_000



