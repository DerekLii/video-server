from datetime import datetime
from flask import Flask, flash, redirect, render_template, request, send_from_directory, url_for
from werkzeug.utils import secure_filename
import glob
import json
import os
import shutil
import subprocess
import uuid

app = Flask(__name__)
app.secret_key = "dev-secret-key"

CONTENT_LIBRARY = "content_library"
LIBRARY_INDEX = os.path.join(CONTENT_LIBRARY, "library.json")
THUMBNAIL_LIBRARY = os.path.join(CONTENT_LIBRARY, "thumbnails")
DEFAULT_CATEGORIES = ["Physique", "Workout", "Volleyball", "Food"]
os.makedirs(CONTENT_LIBRARY, exist_ok=True)
os.makedirs(THUMBNAIL_LIBRARY, exist_ok=True)


def load_library():
    if not os.path.exists(LIBRARY_INDEX):
        return []

    with open(LIBRARY_INDEX, "r", encoding="utf-8") as index_file:
        return json.load(index_file)


def save_library(videos):
    with open(LIBRARY_INDEX, "w", encoding="utf-8") as index_file:
        json.dump(videos, index_file, indent=2)


def parse_tags(raw_tags):
    tags = []
    seen = set()

    for tag in raw_tags.split(","):
        clean_tag = tag.strip().lower()
        tag_key = clean_tag

        if clean_tag and tag_key not in seen:
            tags.append(clean_tag)
            seen.add(tag_key)

    return tags


def normalize_slug(value):
    return secure_filename(value.strip().lower())


def normalize_thumbnail_time(raw_time):
    clean_time = raw_time.strip()

    if not clean_time:
        return "00:00:01"

    if clean_time.isdigit():
        return f"00:00:{int(clean_time):02d}"

    parts = clean_time.split(":")

    if len(parts) in {2, 3} and all(part.isdigit() for part in parts):
        return clean_time

    return "00:00:01"


def library_file_path(video):
    return os.path.join(CONTENT_LIBRARY, *video["path"].replace("\\", "/").split("/"))


def find_ffmpeg():
    ffmpeg_path = shutil.which("ffmpeg")

    if ffmpeg_path:
        return ffmpeg_path

    winget_pattern = os.path.join(
        os.environ.get("LOCALAPPDATA", ""),
        "Microsoft",
        "WinGet",
        "Packages",
        "Gyan.FFmpeg_*",
        "ffmpeg-*",
        "bin",
        "ffmpeg.exe",
    )
    matches = glob.glob(winget_pattern)

    return matches[0] if matches else ""


def create_thumbnail(video_path, video_id, thumbnail_time="00:00:01"):
    ffmpeg_path = find_ffmpeg()

    if not ffmpeg_path:
        return ""

    thumbnail_filename = f"{video_id}.jpg"
    thumbnail_path = os.path.join(THUMBNAIL_LIBRARY, thumbnail_filename)

    try:
        subprocess.run(
            [
                ffmpeg_path,
                "-y",
                "-ss",
                thumbnail_time,
                "-i",
                video_path,
                "-frames:v",
                "1",
                "-q:v",
                "2",
                thumbnail_path,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return ""

    return f"thumbnails/{thumbnail_filename}"


def ensure_thumbnails(videos):
    changed = False

    for video in videos:
        if video.get("thumbnail_path"):
            continue

        video_path = library_file_path(video)

        if not os.path.exists(video_path):
            continue

        thumbnail_path = create_thumbnail(
            video_path,
            video["id"],
            video.get("thumbnail_time", "00:00:01"),
        )

        if thumbnail_path:
            video["thumbnail_path"] = thumbnail_path
            changed = True

    if changed:
        save_library(videos)


def find_video(video_id):
    videos = load_library()

    for video in videos:
        if video["id"] == video_id:
            return videos, video

    return videos, None


def next_daily_sequence(videos, category, date, name):
    prefix = f"{category}_{date}_"
    suffix = f"_{name}"
    highest_sequence = 0

    for video in videos:
        filename = video.get("filename", "")

        if not filename.startswith(prefix):
            continue

        stem, _ = os.path.splitext(filename)

        if not stem.endswith(suffix):
            continue

        sequence = stem.removeprefix(prefix).removesuffix(suffix)

        if sequence.isdigit():
            highest_sequence = max(highest_sequence, int(sequence))

    return highest_sequence + 1


@app.route("/")
def home():
    query = request.args.get("q", "").strip()
    selected_category = request.args.get("category", "").strip()
    selected_tag = request.args.get("tag", "").strip()

    videos = load_library()
    ensure_thumbnails(videos)
    saved_categories = [
        item
        for item in os.listdir(CONTENT_LIBRARY)
        if os.path.isdir(os.path.join(CONTENT_LIBRARY, item))
    ]
    metadata_categories = [video["category"] for video in videos]
    categories = sorted(set(DEFAULT_CATEGORIES + saved_categories + metadata_categories))
    all_tags = sorted({tag for video in videos for tag in video.get("tags", [])}, key=str.lower)

    filtered_videos = videos

    if selected_category:
        filtered_videos = [
            video for video in filtered_videos if video["category"] == selected_category
        ]

    if selected_tag:
        filtered_videos = [
            video for video in filtered_videos if selected_tag in video.get("tags", [])
        ]

    if query:
        query_lower = query.lower()
        filtered_videos = [
            video
            for video in filtered_videos
            if query_lower in video["name"].lower()
            or query_lower in video["category"].lower()
            or query_lower in video["filename"].lower()
            or query_lower in video.get("notes", "").lower()
            or any(query_lower in tag.lower() for tag in video.get("tags", []))
        ]

    filtered_videos = sorted(
        filtered_videos,
        key=lambda video: video.get("uploaded_at", ""),
        reverse=True,
    )

    return render_template(
        "index.html",
        categories=categories,
        videos=filtered_videos,
        all_tags=all_tags,
        query=query,
        selected_category=selected_category,
        selected_tag=selected_tag,
    )


@app.route("/upload", methods=["POST"])
def upload():
    files = [
        file
        for file in request.files.getlist("video")
        if file and file.filename
    ]
    category = request.form.get("category", "").strip()
    new_category = request.form.get("new_category", "").strip()
    thumbnail_time = normalize_thumbnail_time(request.form.get("thumbnail_time", ""))
    name = request.form.get("name", "").strip()
    notes = request.form.get("notes", "").strip()
    thumbnail_time = normalize_thumbnail_time(request.form.get("thumbnail_time", ""))
    tags = parse_tags(request.form.get("tags", ""))

    if not files:
        flash("Choose at least one video before uploading.", "error")
        return redirect(url_for("home"))

    if category == "Other":
        category = new_category

    category = normalize_slug(category)
    name = normalize_slug(name)

    if not category:
        flash("Pick a category or create a new one.", "error")
        return redirect(url_for("home"))

    if not name:
        flash("Add a video name before uploading.", "error")
        return redirect(url_for("home"))

    category_path = os.path.join(CONTENT_LIBRARY, category)
    os.makedirs(category_path, exist_ok=True)

    upload_date = datetime.now().strftime("%Y-%m-%d")
    videos = load_library()
    next_sequence = next_daily_sequence(videos, category, upload_date, name)

    for index, file in enumerate(files, start=1):
        _, extension = os.path.splitext(file.filename)
        extension = extension.lower() or ".mp4"
        sequence = next_sequence + index - 1
        display_name = f"{name.replace('_', ' ')} {sequence}"
        filename = f"{category}_{upload_date}_{sequence:03d}_{name}{extension}"
        save_path = os.path.join(category_path, filename)
        file.save(save_path)
        video_id = uuid.uuid4().hex
        thumbnail_path = create_thumbnail(save_path, video_id, thumbnail_time)

        videos.append(
            {
                "id": video_id,
                "name": display_name,
                "category": category,
                "tags": tags,
                "notes": notes,
                "thumbnail_time": thumbnail_time,
                "filename": filename,
                "path": f"{category}/{filename}",
                "thumbnail_path": thumbnail_path,
                "uploaded_at": datetime.now().isoformat(timespec="seconds"),
                "size": os.path.getsize(save_path),
                "original_filename": file.filename,
            }
        )

    save_library(videos)

    if len(files) == 1:
        flash(f"Uploaded 1 video to {category}.", "success")
    else:
        flash(f"Uploaded {len(files)} videos to {category} with the prefix {name}.", "success")

    return redirect(url_for("home"))


@app.route("/videos/<path:video_path>")
def video_file(video_path):
    return send_from_directory(CONTENT_LIBRARY, video_path)


@app.route("/thumbnails/<path:thumbnail_path>")
def thumbnail_file(thumbnail_path):
    return send_from_directory(THUMBNAIL_LIBRARY, thumbnail_path)


@app.route("/videos/<video_id>/update", methods=["POST"])
def update_video(video_id):
    videos, video = find_video(video_id)

    if not video:
        flash("That video could not be found.", "error")
        return redirect(url_for("home"))

    name = request.form.get("name", "").strip()
    category = request.form.get("category", "").strip()
    new_category = request.form.get("new_category", "").strip()

    if category == "Other":
        category = new_category

    category = normalize_slug(category)
    name = name.lower()

    if not name:
        flash("Add a video name before saving.", "error")
        return redirect(url_for("home"))

    if not category:
        flash("Pick a category or create a new one.", "error")
        return redirect(url_for("home"))

    current_path = library_file_path(video)
    target_category_path = os.path.join(CONTENT_LIBRARY, category)
    os.makedirs(target_category_path, exist_ok=True)

    if category != video["category"]:
        target_path = os.path.join(target_category_path, video["filename"])
        os.replace(current_path, target_path)
        video["path"] = f"{category}/{video['filename']}"

    video["name"] = name
    video["category"] = category
    video["tags"] = parse_tags(request.form.get("tags", ""))
    video["notes"] = request.form.get("notes", "").strip()

    if thumbnail_time != video.get("thumbnail_time"):
        video_path = library_file_path(video)
        thumbnail_path = create_thumbnail(video_path, video["id"], thumbnail_time)

        if thumbnail_path:
            video["thumbnail_path"] = thumbnail_path

        video["thumbnail_time"] = thumbnail_time

    save_library(videos)

    flash(f"Updated {video['name']}.", "success")
    return redirect(url_for("home"))


@app.route("/videos/<video_id>/delete", methods=["POST"])
def delete_video(video_id):
    videos, video = find_video(video_id)

    if not video:
        flash("That video could not be found.", "error")
        return redirect(url_for("home"))

    file_path = library_file_path(video)

    if os.path.exists(file_path):
        os.remove(file_path)

    thumbnail_path = video.get("thumbnail_path")

    if thumbnail_path:
        thumbnail_file_path = os.path.join(
            CONTENT_LIBRARY,
            *thumbnail_path.replace("\\", "/").split("/"),
        )

        if os.path.exists(thumbnail_file_path):
            os.remove(thumbnail_file_path)

    videos = [item for item in videos if item["id"] != video_id]
    save_library(videos)

    flash(f"Deleted {video['name']}.", "success")
    return redirect(url_for("home"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
