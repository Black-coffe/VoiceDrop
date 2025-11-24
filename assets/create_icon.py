"""
Create application icon for VoiceDrop
"""
from PIL import Image, ImageDraw
import os

def create_microphone_icon():
    """Create a simple microphone icon"""
    sizes = [(16, 16), (32, 32), (48, 48), (256, 256)]
    images = []

    for size in sizes:
        # Create image with transparent background
        img = Image.new('RGBA', size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        w, h = size

        # Background circle (dark blue gradient effect)
        margin = int(w * 0.05)
        draw.ellipse([margin, margin, w - margin, h - margin], fill='#2d3748')

        # Microphone body (teal/cyan color)
        mic_width = w * 0.3
        mic_height = h * 0.4
        mic_x = (w - mic_width) / 2
        mic_y = h * 0.2

        # Microphone head (rounded rectangle approximation)
        draw.rounded_rectangle(
            [mic_x, mic_y, mic_x + mic_width, mic_y + mic_height],
            radius=int(mic_width / 2),
            fill='#4ecdc4'
        )

        # Microphone base/stand
        stand_width = w * 0.08
        stand_height = h * 0.15
        stand_x = (w - stand_width) / 2
        stand_y = mic_y + mic_height - 2
        draw.rectangle(
            [stand_x, stand_y, stand_x + stand_width, stand_y + stand_height],
            fill='#4ecdc4'
        )

        # Base
        base_width = w * 0.4
        base_height = h * 0.08
        base_x = (w - base_width) / 2
        base_y = stand_y + stand_height - 2
        draw.rounded_rectangle(
            [base_x, base_y, base_x + base_width, base_y + base_height],
            radius=int(base_height / 2),
            fill='#4ecdc4'
        )

        # Highlight on mic head
        highlight_x = mic_x + mic_width * 0.2
        highlight_y = mic_y + mic_height * 0.2
        highlight_w = mic_width * 0.25
        highlight_h = mic_height * 0.15
        draw.ellipse(
            [highlight_x, highlight_y, highlight_x + highlight_w, highlight_y + highlight_h],
            fill='#7ee8e0'
        )

        images.append(img)

    # Save as ICO with multiple sizes
    images[3].save(
        'icon.ico',
        format='ICO',
        sizes=[(s, s) for s, _ in sizes],
        append_images=images[:3]
    )

    print("Icon created: icon.ico")

if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)) if os.path.dirname(os.path.abspath(__file__)) else '.')
    create_microphone_icon()
