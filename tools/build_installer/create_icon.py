#!/usr/bin/env python3
"""
Converts an image file to Windows .ico format for use as the app icon.

Usage:
    python create_icon.py <input_image> [output_icon.ico]

The input image should be at least 256x256 pixels. Common formats like
PNG, JPG, or BMP are supported.

Generated icon is used by:
- PyInstaller (shyyctl.spec) for the executable icon
- NSIS installer (installer.nsi) for installer graphics
"""

import sys
from pathlib import Path

def create_ico_from_image(image_path: str, output_path: str = None):
    """Convert an image to ICO format using PIL/Pillow."""
    try:
        from PIL import Image
    except ImportError:
        print("Error: Pillow not installed. Install with: pip install Pillow")
        return False

    if output_path is None:
        output_path = Path(image_path).stem + ".ico"

    try:
        img = Image.open(image_path)
        # Ensure image is RGB (not RGBA, which some tools don't handle well in .ico)
        if img.mode in ('RGBA', 'LA', 'P'):
            # Create white background
            bg = Image.new('RGB', img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
            img = bg

        # Resize to 256x256 (standard icon size)
        img = img.resize((256, 256), Image.Resampling.LANCZOS)

        # Save as ICO
        img.save(output_path, format='ICO')
        print(f"✓ Created icon: {output_path}")
        return True
    except Exception as e:
        print(f"✗ Error creating icon: {e}")
        return False

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    input_file = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else None

    if not Path(input_file).exists():
        print(f"Error: File not found: {input_file}")
        sys.exit(1)

    success = create_ico_from_image(input_file, output_file)
    sys.exit(0 if success else 1)
