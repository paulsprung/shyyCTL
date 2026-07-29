# Custom Icon Setup

To add a custom icon to the shyyctl Windows installer and executable:

## 1. Prepare Your Icon Image

- Start with an image at least 256×256 pixels (PNG, JPG, or BMP)
- Should have a transparent or clean background
- Recommended: square aspect ratio

## 2. Convert to ICO Format

Run the icon converter script:

```bash
cd tools/build_installer
python create_icon.py /path/to/your/image.png
```

This creates `shyyctl.ico` in the same directory.

## 3. Update the Installer

Modify `installer.nsi` to use your icon:
- Update the installer header icon: `Icon "path\to\shyyctl.ico"`
- This makes the installer window show your icon

## 4. Build the Release

The release workflow will automatically:
- PyInstaller will detect `shyyctl.ico` and use it for the executable
- The installer will include it as window branding
- Both shyyctl.exe and the setup wizard will display your custom icon

## Files Involved

- `shyyctl.spec` - PyInstaller config (auto-detects icon.ico)
- `installer.nsi` - NSIS installer script (needs manual update for icon reference)
- `create_icon.py` - Utility to convert images to .ico format

## Notes

- If no `shyyctl.ico` is found, PyInstaller uses its default icon
- The icon file should be generated fresh before each release
- Recommended to use a high-quality source image for best results on all display scales
