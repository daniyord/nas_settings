"""The poster of a movie or scene no database knows: its title over a frame of the video, a trailing number set apart
as with a series ("Sneak Peek 9"), and the studio at the bottom (from jellyfin-tpdb-match)."""

import io
import re
import subprocess

from PIL import Image, ImageDraw, ImageFont

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
GOLD = (212, 175, 55)
WIDTH, HEIGHT = 1000, 1500


def font(size):
    try:
        return ImageFont.truetype(FONT, size)
    except OSError:
        return ImageFont.load_default(size)


def video_frame(path, at):
    """The frame at `at` (0-1) of the video's length."""
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        capture_output=True,
        text=True,
        check=True,
    )
    seconds = float(probe.stdout.strip() or 0) * at
    grab = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{seconds:.1f}", "-i", path, "-frames:v", "1"]
        + ["-f", "image2pipe", "-vcodec", "mjpeg", "-"],
        capture_output=True,
        check=True,
    )
    return Image.open(io.BytesIO(grab.stdout)).convert("RGB")


def wrapped(draw, text, width, max_lines, sizes):
    """(lines, font, size): the text in lines of the largest size that fits `width` in `max_lines` (or the
    smallest)."""
    lines, chosen, size = [], font(sizes[-1]), sizes[-1]
    for size in sizes:
        chosen = font(size)
        lines = []
        for word in text.split():
            if lines and draw.textlength(f"{lines[-1]} {word}", font=chosen) <= width:
                lines[-1] += f" {word}"
            else:
                lines.append(word)
        if len(lines) <= max_lines and all(draw.textlength(line, font=chosen) <= width for line in lines):
            break
    return lines, chosen, size


def make(frame, title, studio):
    """The poster as JPEG bytes, around a frame (a PIL image)."""
    poster = Image.new("RGB", (WIDTH, HEIGHT), (20, 20, 20))
    draw = ImageDraw.Draw(poster)
    draw.rectangle([0, 0, WIDTH - 1, 5], fill=GOLD)
    draw.rectangle([0, HEIGHT - 6, WIDTH - 1, HEIGHT - 1], fill=GOLD)
    series = re.fullmatch(r"(.+?)\s+(\d+)", title)
    name, number = (series.group(1), series.group(2)) if series else (title, None)
    lines, text_font, size = wrapped(draw, name.upper(), 900, 3, list(range(120, 45, -5)))
    y = 90
    for line in lines:
        draw.text(((WIDTH - draw.textlength(line, font=text_font)) / 2, y), line, font=text_font, fill="white")
        y += round(size * 1.15)
    if number:
        big = font(230)
        draw.text(((WIDTH - draw.textlength(number, font=big)) / 2, y), number, font=big, fill=GOLD)
        y += 270
    small = font(70)
    label = studio.upper()
    draw.text(((WIDTH - draw.textlength(label, font=small)) / 2, 1350), label, font=small, fill=(217, 217, 217))
    frame = frame.resize((WIDTH, round(frame.height * WIDTH / frame.width)))
    room = 1320 - (y + 30)
    if frame.height > room:
        frame = frame.resize((round(frame.width * room / frame.height), room))
    top = max(y + 30, (HEIGHT - frame.height) // 2 + 60)
    poster.paste(frame, ((WIDTH - frame.width) // 2, min(top, 1320 - frame.height)))
    out = io.BytesIO()
    poster.save(out, "JPEG", quality=88)
    return out.getvalue()
