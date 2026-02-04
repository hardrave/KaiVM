import sys
import os

# Add site-packages to path
sys.path.append('/home/kai/kaivm/.venv/lib/python3.13/site-packages')

try:
    from PIL import Image, ImageDraw, ImageFont
    print("PIL imports successful")
except ImportError as e:
    print(f"PIL import failed: {e}")
    sys.exit(1)

try:
    with Image.open('/run/kaivm/latest.jpg') as img:
        print(f"Size: {img.size}")
except Exception as e:
    print(f"Error: {e}")
