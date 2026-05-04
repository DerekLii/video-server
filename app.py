from datetime import datetime, timedelta
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
TRASH_LIBRARY = os.path.join(CONTENT_LIBRARY, "trash")
DEFAULT_CATEGORIES = ["physique", "workout", "volleyball", "food"]
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
os.makedirs(CONTENT_LIBRARY, exist_ok=True)
os.makedirs(THUMBNAIL_LIBRARY, exist_ok=True)
os.makedirs(TRASH_LIBRARY, exist_ok=True)


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


def parse_collections(raw_collections):
    return parse_tags(raw_collections)


def normalize_slug(value):
    return secure_filename(value.strip().lower())


def display_name_from_filename(filename):
    stem, _ = os.path.splitext(filename)
    return stem.replace("_", " ").replace("-", " ").strip().lower()


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


def video_uploaded_date(video):
    uploaded_at = video.get("uploaded_at", "")

    try:
        return datetime.fromisoformat(uploaded_at).date()
    except ValueError:
        return None


def date_filter_range(date_filter, custom_from, custom_to):
    today = datetime.now().date()

    if date_filter == "today":
        return today, today

    if date_filter == "week":
        return today - timedelta(days=6), today

    if date_filter == "month":
        return today.replace(day=1), today

    if date_filter == "custom":
        try:
            start_date = datetime.fromisoformat(custom_from).date() if custom_from else None
            end_date = datetime.fromisoformat(custom_to).date() if custom_to else None
        except ValueError:
            return None, None

        return start_date, end_date

    return None, None


def matches_date_filter(video, start_date, end_date):
    if not start_date and not end_date:
        return True

    uploaded_date = video_uploaded_date(video)

    if not uploaded_date:
        return False

    if start_date and uploaded_date < start_date:
        return False

    if end_date and uploaded_date > end_date:
        return False

    return True


def unique_trash_path(filename):
    trash_filename = f"{uuid.uuid4().hex}_{filename}"
    return os.path.join(TRASH_LIBRARY, trash_filename), f"trash/{trash_filename}"


def unique_restore_path(video):
    original_path = video.get("original_path") or f"{video['category']}/{video['filename']}"
    restore_path = os.path.join(CONTENT_LIBRARY, *original_path.split("/"))

    if not os.path.exists(restore_path):
        return restore_path, original_path

    directory = os.path.dirname(restore_path)
    stem, extension = os.path.splitext(os.path.basename(restore_path))
    restored_filename = f"{stem}_restored_{uuid.uuid4().hex[:6]}{extension}"
    return os.path.join(directory, restored_filename), f"{video['category']}/{restored_filename}"


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


def find_ffprobe():
    ffprobe_path = shutil.which("ffprobe")

    if ffprobe_path:
        return ffprobe_path

    ffmpeg_path = find_ffmpeg()

    if ffmpeg_path:
        ffprobe_path = os.path.join(os.path.dirname(ffmpeg_path), "ffprobe.exe")

        if os.path.exists(ffprobe_path):
            return ffprobe_path

    return ""


def format_duration(duration_seconds):
    if not duration_seconds:
        return ""

    whole_seconds = int(duration_seconds)
    minutes = whole_seconds // 60
    seconds = whole_seconds % 60

    if minutes < 60:
        return f"{minutes}:{seconds:02d}"

    hours = minutes // 60
    minutes = minutes % 60
    return f"{hours}:{minutes:02d}:{seconds:02d}"


def format_file_size(size):
    if not size:
        return ""

    units = ["B", "KB", "MB", "GB"]
    value = float(size)

    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} {unit}"

        value /= 1024

    return ""


def get_video_duration(video_path):
    ffprobe_path = find_ffprobe()

    if not ffprobe_path:
        return 0

    try:
        result = subprocess.run(
            [
                ffprobe_path,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                video_path,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return 0

    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0


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


def ensure_video_metadata(videos):
    changed = False

    for video in videos:
        video_path = library_file_path(video)

        if not os.path.exists(video_path):
            continue

        size = os.path.getsize(video_path)

        if video.get("size") != size:
            video["size"] = size
            changed = True

        if video.get("size_label") != format_file_size(size):
            video["size_label"] = format_file_size(size)
            changed = True

        if not video.get("duration"):
            duration = get_video_duration(video_path)

            if duration:
                video["duration"] = duration
                video["duration_label"] = format_duration(duration)
                changed = True

        if not video.get("thumbnail_path"):
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


def video_entry_from_file(file_path, category, relative_path):
    video_id = uuid.uuid4().hex
    size = os.path.getsize(file_path)
    duration = get_video_duration(file_path)
    thumbnail_time = "00:00:01"
    thumbnail_path = create_thumbnail(file_path, video_id, thumbnail_time)

    return {
        "id": video_id,
        "name": display_name_from_filename(os.path.basename(file_path)),
        "category": category,
        "tags": [],
        "collections": [],
        "notes": "",
        "thumbnail_time": thumbnail_time,
        "filename": os.path.basename(file_path),
        "path": relative_path,
        "thumbnail_path": thumbnail_path,
        "uploaded_at": datetime.fromtimestamp(os.path.getmtime(file_path)).isoformat(timespec="seconds"),
        "size": size,
        "size_label": format_file_size(size),
        "duration": duration,
        "duration_label": format_duration(duration),
        "original_filename": os.path.basename(file_path),
    }


def library_stats(videos):
    active_videos = [video for video in videos if not video.get("trashed")]
    trashed_videos = [video for video in videos if video.get("trashed")]
    total_size = sum(video.get("size", 0) for video in active_videos)
    total_duration = sum(video.get("duration", 0) for video in active_videos)
    category_counts = {}

    for video in active_videos:
        category = video.get("category", "uncategorized")
        category_counts[category] = category_counts.get(category, 0) + 1

    top_categories = sorted(
        category_counts.items(),
        key=lambda item: (-item[1], item[0]),
    )[:4]

    return {
        "total_videos": len(active_videos),
        "trashed_videos": len(trashed_videos),
        "total_size": format_file_size(total_size),
        "total_duration": format_duration(total_duration),
        "top_categories": top_categories,
    }


@app.route("/")
def home():
    query = request.args.get("q", "").strip()
    selected_category = request.args.get("category", "").strip()
    selected_tag = request.args.get("tag", "").strip()
    selected_collection = request.args.get("collection", "").strip()
    date_filter = request.args.get("date", "").strip()
    custom_from = request.args.get("from", "").strip()
    custom_to = request.args.get("to", "").strip()
    sort = request.args.get("sort", "newest").strip()

    videos = load_library()
    ensure_video_metadata(videos)
    stats = library_stats(videos)
    active_videos = [video for video in videos if not video.get("trashed")]
    trashed_videos = sorted(
        [video for video in videos if video.get("trashed")],
        key=lambda video: video.get("deleted_at", ""),
        reverse=True,
    )
    saved_categories = [
        item
        for item in os.listdir(CONTENT_LIBRARY)
        if os.path.isdir(os.path.join(CONTENT_LIBRARY, item))
        and item.lower() not in {"thumbnails", "trash", ".trash", "__pycache__"}
    ]
    metadata_categories = [video["category"] for video in active_videos]
    categories = sorted(set(DEFAULT_CATEGORIES + saved_categories + metadata_categories))
    all_tags = sorted({tag for video in active_videos for tag in video.get("tags", [])}, key=str.lower)
    all_collections = sorted(
        {
            collection
            for video in active_videos
            for collection in video.get("collections", [])
        },
        key=str.lower,
    )

    filtered_videos = active_videos
    start_date, end_date = date_filter_range(date_filter, custom_from, custom_to)

    if selected_category:
        filtered_videos = [
            video for video in filtered_videos if video["category"] == selected_category
        ]

    if selected_tag:
        filtered_videos = [
            video for video in filtered_videos if selected_tag in video.get("tags", [])
        ]

    if selected_collection:
        filtered_videos = [
            video
            for video in filtered_videos
            if selected_collection in video.get("collections", [])
        ]

    filtered_videos = [
        video
        for video in filtered_videos
        if matches_date_filter(video, start_date, end_date)
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
            or any(
                query_lower in collection.lower()
                for collection in video.get("collections", [])
            )
        ]

    sort_options = {
        "newest": "Newest",
        "oldest": "Oldest",
        "name": "Name",
        "category": "Category",
        "size": "File Size",
        "duration": "Duration",
    }

    if sort not in sort_options:
        sort = "newest"

    sort_keys = {
        "newest": lambda video: video.get("uploaded_at", ""),
        "oldest": lambda video: video.get("uploaded_at", ""),
        "name": lambda video: video.get("name", ""),
        "category": lambda video: video.get("category", ""),
        "size": lambda video: video.get("size", 0),
        "duration": lambda video: video.get("duration", 0),
    }

    filtered_videos = sorted(
        filtered_videos,
        key=sort_keys[sort],
        reverse=sort in {"newest", "size", "duration"},
    )

    return render_template(
        "index.html",
        categories=categories,
        videos=filtered_videos,
        all_tags=all_tags,
        all_collections=all_collections,
        stats=stats,
        trashed_videos=trashed_videos,
        query=query,
        selected_category=selected_category,
        selected_tag=selected_tag,
        selected_collection=selected_collection,
        date_filter=date_filter,
        custom_from=custom_from,
        custom_to=custom_to,
        sort=sort,
        sort_options=sort_options,
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
    name = request.form.get("name", "").strip()
    notes = request.form.get("notes", "").strip()
    thumbnail_time = normalize_thumbnail_time(request.form.get("thumbnail_time", ""))
    tags = parse_tags(request.form.get("tags", ""))
    collections = parse_collections(request.form.get("collections", ""))

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
        duration = get_video_duration(save_path)
        thumbnail_path = create_thumbnail(save_path, video_id, thumbnail_time)

        videos.append(
            {
                "id": video_id,
                "name": display_name,
                "category": category,
                "tags": tags,
                "collections": collections,
                "notes": notes,
                "thumbnail_time": thumbnail_time,
                "filename": filename,
                "path": f"{category}/{filename}",
                "thumbnail_path": thumbnail_path,
                "uploaded_at": datetime.now().isoformat(timespec="seconds"),
                "size": os.path.getsize(save_path),
                "size_label": format_file_size(os.path.getsize(save_path)),
                "duration": duration,
                "duration_label": format_duration(duration),
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


@app.route("/videos/<path:video_path>/download")
def download_video(video_path):
    directory, filename = os.path.split(video_path.replace("\\", "/"))
    return send_from_directory(
        os.path.join(CONTENT_LIBRARY, directory),
        filename,
        as_attachment=True,
    )


@app.route("/thumbnails/<path:thumbnail_path>")
def thumbnail_file(thumbnail_path):
    return send_from_directory(THUMBNAIL_LIBRARY, thumbnail_path)


@app.route("/import-existing", methods=["POST"])
def import_existing():
    videos = load_library()
    known_paths = {video.get("path", "").replace("\\", "/") for video in videos}
    imported_count = 0
    skipped_count = 0

    ignored_dirs = {"thumbnails", "trash", ".trash", "__pycache__"}

    for root, dirs, files in os.walk(CONTENT_LIBRARY):
        dirs[:] = [
            directory
            for directory in dirs
            if directory.lower() not in ignored_dirs
        ]

        if root == CONTENT_LIBRARY:
            continue

        relative_root = os.path.relpath(root, CONTENT_LIBRARY).replace("\\", "/")
        category = normalize_slug(relative_root.split("/")[0])

        if not category:
            continue

        for filename in files:
            _, extension = os.path.splitext(filename)

            if extension.lower() not in VIDEO_EXTENSIONS:
                continue

            relative_path = f"{relative_root}/{filename}".replace("\\", "/")

            if relative_path in known_paths:
                skipped_count += 1
                continue

            file_path = os.path.join(root, filename)
            videos.append(video_entry_from_file(file_path, category, relative_path))
            known_paths.add(relative_path)
            imported_count += 1

    save_library(videos)

    if imported_count:
        flash(f"Imported {imported_count} existing videos. Skipped {skipped_count} already in the library.", "success")
    else:
        flash(f"No new videos found. Skipped {skipped_count} already in the library.", "success")

    return redirect(url_for("home"))


@app.route("/videos/<video_id>/open-folder", methods=["POST"])
def open_video_folder(video_id):
    _, video = find_video(video_id)

    if not video:
        flash("That video could not be found.", "error")
        return redirect(url_for("home"))

    file_path = library_file_path(video)
    folder_path = os.path.dirname(file_path)

    if not os.path.exists(folder_path):
        flash("That folder could not be found.", "error")
        return redirect(url_for("home"))

    os.startfile(folder_path)
    flash(f"Opened folder for {video['name']}.", "success")
    return redirect(url_for("home"))


@app.route("/videos/<video_id>/update", methods=["POST"])
def update_video(video_id):
    videos, video = find_video(video_id)

    if not video:
        flash("That video could not be found.", "error")
        return redirect(url_for("home"))

    name = request.form.get("name", "").strip()
    category = request.form.get("category", "").strip()
    new_category = request.form.get("new_category", "").strip()
    thumbnail_time = normalize_thumbnail_time(request.form.get("thumbnail_time", ""))

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
    video["collections"] = parse_collections(request.form.get("collections", ""))
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

    if video.get("trashed"):
        flash("That video is already in trash.", "success")
        return redirect(url_for("home"))

    file_path = library_file_path(video)

    if os.path.exists(file_path):
        trash_path, trash_relative_path = unique_trash_path(video["filename"])
        os.replace(file_path, trash_path)
        video["original_path"] = video.get("path", "")
        video["path"] = trash_relative_path

    video["trashed"] = True
    video["deleted_at"] = datetime.now().isoformat(timespec="seconds")
    save_library(videos)

    flash(f"Moved {video['name']} to trash.", "success")
    return redirect(url_for("home"))


@app.route("/videos/<video_id>/restore", methods=["POST"])
def restore_video(video_id):
    videos, video = find_video(video_id)

    if not video:
        flash("That video could not be found.", "error")
        return redirect(url_for("home"))

    if not video.get("trashed"):
        flash("That video is already in the library.", "success")
        return redirect(url_for("home"))

    trash_path = library_file_path(video)

    if not os.path.exists(trash_path):
        flash("That trashed video file could not be found.", "error")
        return redirect(url_for("home"))

    restore_path, restore_relative_path = unique_restore_path(video)
    os.makedirs(os.path.dirname(restore_path), exist_ok=True)
    os.replace(trash_path, restore_path)

    video["path"] = restore_relative_path
    video["filename"] = os.path.basename(restore_relative_path)
    video["trashed"] = False
    video.pop("deleted_at", None)
    video.pop("original_path", None)
    save_library(videos)

    flash(f"Restored {video['name']}.", "success")
    return redirect(url_for("home"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
