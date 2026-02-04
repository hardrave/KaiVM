import sys
sys.path.append('/home/kai/kaivm/.venv/lib/python3.13/site-packages')
from PIL import Image
import numpy as np

def analyze():
    try:
        img = Image.open('/run/kaivm/latest.jpg').convert('RGB')
        arr = np.array(img)
        h, w, _ = arr.shape
        
        # Check for black bars (threshold 20)
        # Scan rows
        top_bar = 0
        for y in range(h):
            if np.mean(arr[y, :, :]) > 20:
                top_bar = y
                break
                
        bottom_bar = h
        for y in range(h-1, -1, -1):
            if np.mean(arr[y, :, :]) > 20:
                bottom_bar = y + 1
                break
                
        # Scan cols
        left_bar = 0
        for x in range(w):
            if np.mean(arr[:, x, :]) > 20:
                left_bar = x
                break
                
        right_bar = w
        for x in range(w-1, -1, -1):
            if np.mean(arr[:, x, :]) > 20:
                right_bar = x + 1
                break
                
        print(f"Image Size: {w}x{h}")
        print(f"Detected Content: Left={left_bar}, Top={top_bar}, Right={right_bar}, Bottom={bottom_bar}")
        print(f"Active Area: {right_bar - left_bar}x{bottom_bar - top_bar}")
        
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    analyze()
