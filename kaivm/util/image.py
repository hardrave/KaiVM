from io import BytesIO
from typing import Tuple, Optional

from kaivm.util.log import get_logger

log = get_logger("kaivm.util.image")

try:
    from PIL import Image, ImageDraw, ImageFont
    HAS_PIL = True
except ImportError:
    HAS_PIL = False
    log.warning("PIL not installed/importable; image processing disabled.")

def get_image_size(jpeg_bytes: bytes) -> Tuple[int, int]:
    """Returns (width, height). Returns (1280, 720) if PIL missing or error."""
    if not HAS_PIL:
        return (1280, 720)
    try:
        with Image.open(BytesIO(jpeg_bytes)) as img:
            return img.size
    except Exception as e:
        log.warning(f"Failed to get image size from bytes ({len(jpeg_bytes)}B): {e}")
        return (1280, 720)

def process_image(jpeg_bytes: bytes, max_dim: int = 1024) -> Tuple[bytes, int, int]:
    """
    Resizes image to have max dimension `max_dim` (maintaining aspect ratio),
    applies grid overlay, and returns (processed_bytes, width, height).
    """
    if not HAS_PIL:
        return (jpeg_bytes, 1280, 720)

    try:
        img = Image.open(BytesIO(jpeg_bytes)).convert("RGB")
        w, h = img.size

        # Resize if needed
        if w > max_dim or h > max_dim:
            if w > h:
                new_w = max_dim
                new_h = int(h * (max_dim / w))
            else:
                new_h = max_dim
                new_w = int(w * (max_dim / h))
            img = img.resize((new_w, new_h), Image.Resampling.BICUBIC)
        else:
            new_w, new_h = w, h

        # Draw Grid
        draw = ImageDraw.Draw(img)
        
        # Grid settings
        cols = 10
        rows = 10
        
        step_x = new_w / cols
        step_y = new_h / rows
        
        # Overlay for transparency
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw_ov = ImageDraw.Draw(overlay)
        
        try:
            font = ImageFont.truetype("DejaVuSans.ttf", 14)
        except OSError:
            try:
                font = ImageFont.truetype("arial.ttf", 14)
            except OSError:
                font = ImageFont.load_default()

        # High-contrast grid
        line_color = (255, 0, 0, 160)
        text_color = (0, 0, 0, 255)
        bg_color = (255, 255, 255, 220)

        # Draw X grid lines (vertical)
        for i in range(0, cols + 1):
            x = int(i * step_x)
            if x >= new_w: x = new_w - 1
            draw_ov.line([(x, 0), (x, new_h)], fill=line_color, width=2)
            
            # Label: 0 to 1000
            val = i * 100
            if i > 0 and i < cols: # Avoid edges if desired, or keep them
                text = str(val)
                bbox = draw_ov.textbbox((x, 5), text, font=font)
                # Expand background
                bbox = (bbox[0]-2, bbox[1]-2, bbox[2]+2, bbox[3]+2)
                draw_ov.rectangle(bbox, fill=bg_color)
                draw_ov.text((x, 5), text, fill=text_color, font=font, anchor="mt")

        # Draw Y grid lines (horizontal)
        for i in range(0, rows + 1):
            y = int(i * step_y)
            if y >= new_h: y = new_h - 1
            draw_ov.line([(0, y), (new_w, y)], fill=line_color, width=2)
            
            # Label: 0 to 1000
            val = i * 100
            if i > 0 and i < rows:
                text = str(val)
                bbox = draw_ov.textbbox((5, y), text, font=font)
                bbox = (bbox[0]-2, bbox[1]-2, bbox[2]+2, bbox[3]+2)
                draw_ov.rectangle(bbox, fill=bg_color)
                draw_ov.text((5, y), text, fill=text_color, font=font, anchor="lm")

        # Composite
        img = img.convert("RGBA")
        img = Image.alpha_composite(img, overlay)
        img = img.convert("RGB")
        
        out = BytesIO()
        img.save(out, format="JPEG", quality=80) # Lower quality slightly for speed
        return out.getvalue(), new_w, new_h

    except Exception as e:
        log.warning(f"Failed to process image: {e}")
        # Fallback
        return jpeg_bytes, 1280, 720

# Keep old alias for compatibility if needed, but client will switch to process_image
def add_grid_overlay(jpeg_bytes: bytes) -> bytes:
    b, _, _ = process_image(jpeg_bytes)
    return b