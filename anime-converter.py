#!/usr/bin/env python3

"""Automatic anime video converter."""

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional


# ============================================================
# ОСНОВНЫЕ ПУТИ
# ============================================================

ROOT = Path("/home/chaser/data/jormungandr")
COMPLETE = ROOT / "transmission" / "complete"
ONGOING = ROOT / "ongoing"
LOCK_FILE = ROOT / ".anime-converter.lock"


# ============================================================
# ПАРАМЕТРЫ РАБОТЫ
# ============================================================

SCAN_INTERVAL = 30
STABILITY_DELAY = 15
MIN_FILE_AGE = 30


# ============================================================
# ПОДДЕРЖИВАЕМЫЕ РАСШИРЕНИЯ
# ============================================================

VIDEO_EXTENSIONS = {
    ".mkv",
    ".mp4",
    ".avi",
    ".m4v",
    ".mov",
    ".ts",
    ".webm",
    ".flv",
}

SUBTITLE_EXTENSIONS = {
    ".ass",
    ".ssa",
    ".srt",
    ".vtt",
    ".sup",
    ".sub",
}

AUDIO_EXTENSIONS = {
    ".mka",
    ".aac",
    ".mp3",
    ".ac3",
    ".eac3",
    ".flac",
    ".wav",
    ".ogg",
    ".opus",
}


# ============================================================
# НАЗВАНИЯ СПЕЦИАЛЬНЫХ КАТАЛОГОВ
# ============================================================

AUDIO_DIR_NAMES = {
    "audio",
    "audios",
    "sound",
    "sounds",
    "voice",
    "voices",
    "dub",
    "dubs",
    "озвучка",
    "аудио",
}

SUBTITLE_DIR_NAMES = {
    "sub",
    "subs",
    "subtitle",
    "subtitles",
    "субтитры",
}


# ============================================================
# ЯЗЫКОВЫЕ МЕТКИ
# ============================================================

LANGUAGES = {
    "ru": (
        "ru",
        "rus",
        "russian",
        "рус",
        "русский",
    ),
    "jp": (
        "ja",
        "jp",
        "jpn",
        "japanese",
        "яп",
        "японский",
    ),
    "en": (
        "en",
        "eng",
        "english",
    ),
}


# Чем меньше число, тем выше приоритет.
#
# Основной порядок:
#
#     RU -> JP -> EN
#
LANGUAGE_PRIORITY = {
    "ru": 0,
    "jp": 1,
    "en": 2,
}


# ============================================================
# ИСКЛЮЧЕНИЯ
# ============================================================

class ProbeError(RuntimeError):
    """ffprobe не смог получить корректную информацию о файле."""


# ============================================================
# ЛОГИРОВАНИЕ
# ============================================================

def log(message: str) -> None:
    """Печатает сообщение с текущей датой и временем."""

    print(
        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}",
        flush=True,
    )


# ============================================================
# ЗАПУСК ВНЕШНИХ КОМАНД
# ============================================================

def run_command(
    command: list[str],
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """
    Запускает внешнюю программу.

    Команда передаётся списком аргументов, без shell=True.
    Это исключает shell injection через имена файлов.
    """

    return subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=check,
    )


# ============================================================
# FFPROBE
# ============================================================

def ffprobe_json(path: Path) -> dict[str, Any]:
    """
    Получает техническую информацию о файле через ffprobe.

    При любой ошибке ffprobe выбрасывается ProbeError.

    Важно:
        ошибка ffprobe НЕ означает отсутствие дорожек.
    """

    try:
        result = run_command(
            [
                "ffprobe",
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_streams",
                "-show_format",
                str(path),
            ]
        )

        data = json.loads(result.stdout)

    except (
        OSError,
        subprocess.SubprocessError,
        json.JSONDecodeError,
    ) as exc:
        raise ProbeError(
            f"ffprobe failed for {path}: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise ProbeError(
            f"ffprobe returned invalid JSON for {path}"
        )

    if not isinstance(
        data.get("streams", []),
        list,
    ):
        raise ProbeError(
            f"ffprobe returned invalid streams data for {path}"
        )

    return data


# ============================================================
# НОРМАЛИЗАЦИЯ ТЕКСТА
# ============================================================

def normalize_text(value: str) -> str:
    """
    Приводит строку к удобному для сравнения виду.

    Например:

        Episode_01-RUS

    превращается примерно в:

        episode 01 rus
    """

    value = value.lower()
    value = value.replace("_", " ")
    value = value.replace("-", " ")

    return value


# ============================================================
# ОПРЕДЕЛЕНИЕ ЯЗЫКА
# ============================================================

def detect_language(text: str) -> Optional[str]:
    """
    Пытается определить язык по тексту.

    Возвращает:

        ru
        jp
        en

    или None, если язык определить не удалось.
    """

    text = normalize_text(text)

    for language, tags in LANGUAGES.items():
        for tag in tags:
            if re.search(
                rf"(?<![a-zа-я]){re.escape(tag)}(?![a-zа-я])",
                text,
            ):
                return language

    return None


# ============================================================
# ОПРЕДЕЛЕНИЕ ЯЗЫКА STREAM
# ============================================================

def stream_language(
    stream: dict[str, Any],
) -> Optional[str]:
    """
    Определяет язык внутреннего audio/subtitle stream.

    В первую очередь используются:

        tags.language
        tags.title

    Затем проверяется codec_name.
    """

    tags = stream.get(
        "tags",
        {},
    )

    if not isinstance(tags, dict):
        tags = {}

    candidates = [
        tags.get("language", ""),
        tags.get("title", ""),
        stream.get("codec_name", ""),
    ]

    for value in candidates:
        language = detect_language(
            str(value)
        )

        if language:
            return language

    return None


# ============================================================
# ПОИСК ВНУТРЕННИХ ДОРОЖЕК
# ============================================================

def find_internal_tracks(
    path: Path,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    """
    Ищет внутри исходного видео:

        - аудиодорожки;
        - субтитры.

    Используются только:

        Russian
        Japanese
        English

    Результаты сортируются:

        RU -> JP -> EN

    ВАЖНО:

        Ошибка ffprobe не превращается в
        "пустой список дорожек".

        Вместо этого выбрасывается ProbeError.
    """

    data = ffprobe_json(path)

    audio: list[dict[str, Any]] = []
    subtitles: list[dict[str, Any]] = []

    for stream in data.get(
        "streams",
        [],
    ):
        if not isinstance(stream, dict):
            continue

        language = stream_language(
            stream
        )

        if language not in LANGUAGE_PRIORITY:
            continue

        tags = stream.get(
            "tags",
            {},
        )

        if not isinstance(tags, dict):
            tags = {}

        item = {
            "index": stream.get("index"),
            "language": language,
            "title": tags.get(
                "title",
                "",
            ),
        }

        if stream.get(
            "codec_type"
        ) == "audio":
            audio.append(item)

        elif stream.get(
            "codec_type"
        ) == "subtitle":
            subtitles.append(item)

    audio.sort(
        key=lambda item: LANGUAGE_PRIORITY[
            item["language"]
        ]
    )

    subtitles.sort(
        key=lambda item: LANGUAGE_PRIORITY[
            item["language"]
        ]
    )

    return audio, subtitles


# ============================================================
# ПОИСК НОМЕРА ЭПИЗОДА
# ============================================================

def episode_numbers(
    name: str,
) -> list[str]:
    """
    Извлекает только явно обозначенные номера эпизодов.

    Поддерживаются:

        Episode 01
        Ep 01
        E01

    ВАЖНО:

        Агрессивный fallback по любым числам удалён.

        Например:

            Anime 01 1080p x264

        больше НЕ будет ошибочно разобрано как:

            01
            264

        Диапазоны эпизодов намеренно не поддерживаются,
        так как такие файлы в используемой структуре
        отсутствуют.
    """

    normalized = normalize_text(name)

    result: list[str] = []

    pattern = (
        r"\b(?:episode|ep|e)\s*"
        r"0*(\d{1,4})\b"
    )

    for match in re.finditer(
        pattern,
        normalized,
    ):
        value = match.group(1)

        if value not in result:
            result.append(value)

    return result


# ============================================================
# НОМЕР СЕЗОНА
# ============================================================

def season_numbers(
    name: str,
) -> list[str]:
    """
    Извлекает явные номера сезонов.

    Например:

        S01
        S02
        Season 1
    """

    normalized = normalize_text(name)

    result: list[str] = []

    pattern = (
        r"\b(?:s|season)\s*"
        r"0*(\d{1,3})\b"
    )

    for match in re.finditer(
        pattern,
        normalized,
    ):
        value = match.group(1)

        if value not in result:
            result.append(value)

    return result


# ============================================================
# НОРМАЛИЗОВАННОЕ ИМЯ
# ============================================================

def normalized_stem(
    path: Path,
) -> str:
    """
    Получает нормализованное имя файла.

    Убираются:

        - языковые обозначения;
        - содержимое [...] ;
        - содержимое (...) ;
        - лишние спецсимволы.

    Номер эпизода и сезон здесь не удаляются.
    """

    text = normalize_text(
        path.stem
    )

    # Убираем известные языковые метки.
    text = re.sub(
        r"\b(?:rus|ru|russian|рус|русский|"
        r"jpn|jp|ja|japanese|яп|японский|"
        r"eng|en|english)\b",
        " ",
        text,
    )

    # Убираем содержимое квадратных скобок.
    text = re.sub(
        r"\[[^\]]*\]",
        " ",
        text,
    )

    # Убираем содержимое круглых скобок.
    text = re.sub(
        r"\([^)]*\)",
        " ",
        text,
    )

    # Оставляем только буквы и цифры.
    text = re.sub(
        r"[^a-zа-я0-9]+",
        " ",
        text,
    )

    return " ".join(
        text.split()
    )


# ============================================================
# ИМЯ СЕРИИ
# ============================================================

def series_signature(
    path: Path,
) -> str:
    """
    Получает нормализованное имя anime/series.

    Убираются только известные служебные части:

        - язык;
        - Episode/Ep/E + номер;
        - Season/S + номер.

    ВАЖНО:

        Никаких substring matching и частичного
        совпадения token sets здесь нет.

    Например:

        Anime A - Episode 01.mkv

    и:

        Anime A Something Else - Episode 01.RUS.mka

    НЕ считаются одной серией.

    Это намеренно консервативное поведение:
    лучше не подключить внешний track,
    чем подключить track от другого anime.
    """

    text = normalized_stem(
        path
    )

    # Убираем Episode 01 / Ep 01 / E01.
    text = re.sub(
        r"\b(?:episode|ep|e)\s*\d{1,4}\b",
        " ",
        text,
    )

    # Убираем Season 1 / S01.
    text = re.sub(
        r"\b(?:season|s)\s*\d{1,3}\b",
        " ",
        text,
    )

    return " ".join(
        text.split()
    )


# ============================================================
# ОЦЕНКА СХОЖЕСТИ ФАЙЛОВ
# ============================================================

def similarity_score(
    video: Path,
    candidate: Path,
) -> int:
    """
    Оценивает вероятность того, что candidate относится
    именно к video.

    Главный принцип:

        series signature должен совпадать ТОЧНО.

    Частичное совпадение имён запрещено.
    """

    video_series = series_signature(
        video
    )

    candidate_series = series_signature(
        candidate
    )

    # Если невозможно определить series,
    # автоматическое сопоставление небезопасно.
    if not video_series or not candidate_series:
        return -1000

    # КРИТИЧЕСКОЕ ИСПРАВЛЕНИЕ:
    #
    # Раньше здесь разрешалось:
    #
    #   video_series in candidate_series
    #
    # или:
    #
    #   candidate_series in video_series
    #
    # Это позволяло подключить track от:
    #
    #   Anime A Something Else
    #
    # к:
    #
    #   Anime A
    #
    # Теперь требуется строгое совпадение.
    if video_series != candidate_series:
        return -1000

    score = 100

    # ========================================================
    # SEASON
    # ========================================================

    video_seasons = set(
        season_numbers(
            video.stem
        )
    )

    candidate_seasons = set(
        season_numbers(
            candidate.stem
        )
    )

    # Если сезон явно указан в обоих файлах,
    # он обязан совпадать.
    if (
        video_seasons
        and candidate_seasons
    ):
        if not (
            video_seasons
            & candidate_seasons
        ):
            return -1000

        score += 20

    # ========================================================
    # EPISODE
    # ========================================================

    video_episodes = set(
        episode_numbers(
            video.stem
        )
    )

    candidate_episodes = set(
        episode_numbers(
            candidate.stem
        )
    )

    # Если номер эпизода определён у обоих файлов,
    # он обязан совпадать.
    if (
        video_episodes
        and candidate_episodes
    ):
        if not (
            video_episodes
            & candidate_episodes
        ):
            return -1000

        score += 30

    # Если видео содержит явный номер эпизода,
    # а candidate его не содержит,
    # безопаснее отказаться от автоматического выбора.
    elif (
        video_episodes
        and not candidate_episodes
    ):
        return -1000

    return score


# ============================================================
# ЯЗЫК ВНЕШНЕГО ФАЙЛА
# ============================================================

def candidate_language(
    path: Path,
) -> Optional[str]:
    """
    Определяет язык внешнего файла по его имени.

    Например:

        Episode.01.RUS.mka

    -> ru
    """

    return detect_language(
        path.name
    )


# ============================================================
# ПОИСК КАТАЛОГОВ ДЛЯ ВНЕШНИХ ДОРОЖЕК
# ============================================================

def external_search_directories(
    video: Path,
    directory_names: set[str],
) -> list[Path]:
    """
    Возвращает безопасные каталоги для поиска
    внешних дорожек.

    Разрешены только:

        1. каталог самого видео;
        2. специальные каталоги, являющиеся
           непосредственными дочерними каталогами
           каталога видео.
    """

    directories = [
        video.parent
    ]

    try:
        for child in video.parent.iterdir():

            # Симлинки не используются
            # как каталоги внешних дорожек.
            if child.is_symlink():
                continue

            if not child.is_dir():
                continue

            if (
                child.name.lower()
                not in directory_names
            ):
                continue

            directories.append(
                child
            )

    except OSError as exc:
        log(
            "Unable to inspect external-track "
            f"directories near {video}: {exc}"
        )

    return directories


# ============================================================
# ПОИСК ВНЕШНИХ ДОРОЖЕК
# ============================================================

def find_external_tracks(
    video: Path,
    extension_set: set[str],
    directory_names: set[str],
) -> list[
    tuple[int, Path, Optional[str]]
]:
    """
    Ищет внешние audio/subtitle только
    в безопасном контексте.

    Для каждого кандидата проверяются:

        - symlink;
        - расширение;
        - язык;
        - series;
        - season;
        - episode.
    """

    candidates: list[
        tuple[int, Path, Optional[str]]
    ] = []

    search_dirs = external_search_directories(
        video,
        directory_names,
    )

    seen: set[Path] = set()

    for search_dir in search_dirs:

        try:
            entries = list(
                search_dir.iterdir()
            )

        except OSError:
            continue

        for candidate in entries:

            # =================================================
            # SYMLINK PROTECTION
            # =================================================
            #
            # Даже если symlink указывает на допустимый
            # extension, он не должен использоваться
            # как внешний input.
            #
            if candidate.is_symlink():
                continue

            try:
                candidate_key = candidate.resolve()

            except OSError:
                candidate_key = candidate

            if candidate_key in seen:
                continue

            seen.add(
                candidate_key
            )

            if not candidate.is_file():
                continue

            if (
                candidate.suffix.lower()
                not in extension_set
            ):
                continue

            if candidate == video:
                continue

            # =================================================
            # ЯЗЫК
            # =================================================

            language = candidate_language(
                candidate
            )

            if language not in LANGUAGE_PRIORITY:
                continue

            # =================================================
            # MATCHING
            # =================================================

            score = similarity_score(
                video,
                candidate,
            )

            # Консервативный порог.
            if score < 80:
                continue

            # Специальный каталог даёт небольшой бонус.
            #
            # Он НЕ заменяет matching по series/episode.
            if (
                candidate.parent
                != video.parent
            ):
                score += 10

            candidates.append(
                (
                    score,
                    candidate,
                    language,
                )
            )

    # Сначала score.
    # При одинаковом score — RU -> JP -> EN.
    candidates.sort(
        key=lambda item: (
            -item[0],
            LANGUAGE_PRIORITY.get(
                item[2],
                99,
            ),
            str(item[1]),
        )
    )

    return candidates


# ============================================================
# ВЫБОР ЛУЧШЕГО ВНЕШНЕГО ФАЙЛА
# ============================================================

def select_external_track(
    video: Path,
    extension_set: set[str],
    directory_names: set[str],
) -> Optional[Path]:
    """
    Выбирает лучший внешний audio/subtitle.

    Если несколько кандидатов одного языка имеют
    одинаковый максимальный score, автоматический выбор
    отменяется как неоднозначный.
    """

    candidates = find_external_tracks(
        video,
        extension_set,
        directory_names,
    )

    if not candidates:
        return None

    best_score = candidates[0][0]

    best_candidates = [
        item
        for item in candidates
        if item[0] == best_score
    ]

    languages: dict[
        str,
        list[
            tuple[int, Path, Optional[str]]
        ],
    ] = {}

    for item in best_candidates:
        language = item[2]

        if language is None:
            continue

        languages.setdefault(
            language,
            [],
        ).append(item)

    for language, items in languages.items():

        if len(items) > 1:
            log(
                "Ambiguous external track candidates "
                f"for {video}, language={language}: "
                + ", ".join(
                    str(item[1])
                    for item in items
                )
            )

            return None

    (
        best_score,
        best_path,
        best_language,
    ) = candidates[0]

    log(
        f"Selected external track: "
        f"{best_path} "
        f"(language={best_language}, "
        f"score={best_score})"
    )

    return best_path


# ============================================================
# ПРОВЕРКА СТАБИЛЬНОСТИ ФАЙЛА
# ============================================================

def is_stable(
    path: Path,
) -> bool:
    """
    Проверяет, что файл перестал изменяться.

    Проверяются:

        - возраст;
        - размер;
        - mtime_ns.

    Сравнение mtime добавлено для защиты от ситуации,
    когда содержимое файла изменилось, но его размер
    остался прежним.
    """

    try:
        first_stat = path.stat()

    except OSError:
        return False

    age = (
        time.time()
        - first_stat.st_mtime
    )

    if age < MIN_FILE_AGE:
        return False

    time.sleep(
        STABILITY_DELAY
    )

    try:
        second_stat = path.stat()

    except OSError:
        return False

    return (
        first_stat.st_size
        == second_stat.st_size
        and first_stat.st_mtime_ns
        == second_stat.st_mtime_ns
    )


# ============================================================
# OUTPUT PATH
# ============================================================

def output_path_for(
    source: Path,
) -> Path:
    """
    Строит путь готового файла.

    Структура каталогов внутри COMPLETE
    сохраняется внутри ONGOING.

    Например:

        complete/Anime/Episode 01.mkv

    превращается в:

        ongoing/Anime/Episode 01.mkv
    """

    relative = source.relative_to(
        COMPLETE
    )

    return ONGOING / relative.with_suffix(
        ".mkv"
    )


# ============================================================
# ПРОВЕРКА ГОТОВОГО OUTPUT
# ============================================================

def validate_output(
    path: Path,
) -> bool:
    """
    Проверяет готовый output через ffprobe.

    Проверяем:

        - файл существует;
        - ffprobe может его прочитать;
        - присутствует видеопоток;
        - codec = H.264;
        - высота <= 720;
        - ширина чётная;
        - присутствует audio;
        - все audio = MP3.
    """

    if not path.exists():
        log(
            f"Output does not exist: {path}"
        )
        return False

    try:
        data = ffprobe_json(
            path
        )

    except ProbeError as exc:
        log(
            f"Output validation failed "
            f"for {path}: {exc}"
        )
        return False

    streams = data.get(
        "streams",
        [],
    )

    # ========================================================
    # VIDEO
    # ========================================================

    video_streams = [
        stream
        for stream in streams
        if stream.get("codec_type")
        == "video"
    ]

    if not video_streams:
        log(
            f"Output has no video stream: {path}"
        )
        return False

    video_stream = video_streams[0]

    if (
        video_stream.get("codec_name")
        != "h264"
    ):
        log(
            f"Output video codec is not H.264: "
            f"{path}"
        )
        return False

    width = video_stream.get(
        "width"
    )

    height = video_stream.get(
        "height"
    )

    if not width or not height:
        log(
            f"Output has invalid video dimensions: "
            f"{path}"
        )
        return False

    if height > 720:
        log(
            f"Output height is greater than 720: "
            f"{width}x{height}"
        )
        return False

    if width % 2 != 0:
        log(
            f"Output width is not even: "
            f"{width}x{height}"
        )
        return False

    # ========================================================
    # AUDIO
    # ========================================================

    audio_streams = [
        stream
        for stream in streams
        if stream.get("codec_type")
        == "audio"
    ]

    if not audio_streams:
        log(
            f"Output has no audio stream: {path}"
        )
        return False

    for stream in audio_streams:

        if stream.get(
            "codec_name"
        ) != "mp3":
            log(
                f"Output contains non-MP3 "
                f"audio: {path}"
            )
            return False

    return True


# ============================================================
# СОЗДАНИЕ КОМАНДЫ FFMPEG
# ============================================================

def build_ffmpeg_command(
    source: Path,
    output: Path,
    audio_tracks: list[dict[str, Any]],
    subtitle_tracks: list[dict[str, Any]],
    external_audio: Optional[Path],
    external_subtitle: Optional[Path],
) -> list[str]:
    """
    Создаёт полный набор параметров ffmpeg.

    ВАЖНО:

        ffprobe здесь больше НЕ вызывается.

    Информация о внутренних дорожках уже была получена
    в process_video() и передаётся сюда готовыми списками.
    """

    command = [
        "ffmpeg",

        "-hide_banner",

        "-loglevel",
        "warning",

        "-y",

        "-i",
        str(source),
    ]

    # --------------------------------------------------------
    # ВНЕШНИЙ AUDIO
    # --------------------------------------------------------

    if external_audio:
        command += [
            "-i",
            str(external_audio),
        ]

    # --------------------------------------------------------
    # ВНЕШНИЕ SUBTITLES
    # --------------------------------------------------------

    if external_subtitle:
        command += [
            "-i",
            str(external_subtitle),
        ]

    # --------------------------------------------------------
    # VIDEO
    # --------------------------------------------------------

    command += [
        "-map",
        "0:v:0",
    ]

    # --------------------------------------------------------
    # INTERNAL AUDIO
    # --------------------------------------------------------

    for track in audio_tracks:
        command += [
            "-map",
            f"0:{track['index']}",
        ]

    # --------------------------------------------------------
    # EXTERNAL AUDIO
    # --------------------------------------------------------

    # Внешний audio input идёт после source,
    # поэтому его индекс всегда 1.
    if external_audio:
        command += [
            "-map",
            "1:a:0",
        ]

    # --------------------------------------------------------
    # INTERNAL SUBTITLES
    # --------------------------------------------------------

    for track in subtitle_tracks:
        command += [
            "-map",
            f"0:{track['index']}",
        ]

    # --------------------------------------------------------
    # EXTERNAL SUBTITLES
    # --------------------------------------------------------

    if external_subtitle:

        subtitle_input_index = (
            1
            + int(bool(external_audio))
        )

        command += [
            "-map",
            f"{subtitle_input_index}:s:0",
        ]

    # ========================================================
    # VIDEO SCALE
    # ========================================================
    #
    # input <= 720p -> исходная высота
    # input > 720p  -> высота 720
    #
    # -2 автоматически рассчитывает чётную ширину.
    # ========================================================

    command += [
        "-vf",
        (
            "scale="
            "'if(gt(ih,720),-2,iw)':"
            "'if(gt(ih,720),720,ih)'"
        ),

        "-c:v",
        "libx264",

        "-preset",
        "veryfast",

        "-c:a",
        "libmp3lame",

        "-b:a",
        "192k",

        "-c:s",
        "copy",

        "-map_metadata",
        "0",

        "-map_chapters",
        "0",
    ]

    # ========================================================
    # DEFAULT AUDIO
    # ========================================================
    #
    # КРИТИЧЕСКОЕ ИСПРАВЛЕНИЕ:
    #
    # ffmpeg может сохранить disposition, полученный
    # из исходных streams.
    #
    # Поэтому сначала сбрасываем default у ВСЕХ
    # output audio streams, затем ставим default
    # только первой дорожке.
    # ========================================================

    audio_count = (
        len(audio_tracks)
        + int(bool(external_audio))
    )

    for index in range(audio_count):
        command += [
            f"-disposition:a:{index}",
            "0",
        ]

    if audio_count:
        command += [
            "-disposition:a:0",
            "default",
        ]

    # ========================================================
    # DEFAULT SUBTITLE
    # ========================================================
    #
    # Аналогично audio:
    #
    #   сначала очищаем все dispositions;
    #   затем первая subtitle = default.
    # ========================================================

    subtitle_count = (
        len(subtitle_tracks)
        + int(bool(external_subtitle))
    )

    for index in range(subtitle_count):
        command += [
            f"-disposition:s:{index}",
            "0",
        ]

    if subtitle_count:
        command += [
            "-disposition:s:0",
            "default",
        ]

    command.append(
        str(output)
    )

    return command


# ============================================================
# БЕЗОПАСНОЕ УДАЛЕНИЕ ФАЙЛА
# ============================================================

def remove_file_safely(
    path: Path,
    description: str,
) -> bool:
    """
    Удаляет только указанный файл.

    Ошибка удаления не приводит к исключению
    из основного workflow.
    """

    try:
        path.unlink()

    except OSError as exc:
        log(
            f"Cannot remove {description} "
            f"{path}: {exc}"
        )
        return False

    return True


# ============================================================
# БЕЗОПАСНАЯ ОБРАБОТКА ОДНОГО ВИДЕО
# ============================================================

def process_video(
    video: Path,
    dry_run: bool = False,
) -> None:
    """
    Обрабатывает одно видео.

    В обычном режиме:

        1. определяет output;
        2. проверяет стабильность;
        3. один раз вызывает ffprobe;
        4. определяет internal tracks;
        5. определяет external tracks;
        6. создаёт .processing;
        7. запускает ffmpeg;
        8. проверяет .processing;
        9. делает atomic rename;
        10. проверяет final output;
        11. повторно проверяет source;
        12. удаляет source.

    При любой ошибке source остаётся.
    """

    # ========================================================
    # OUTPUT
    # ========================================================

    output = output_path_for(
        video
    )

    # Если валидный output уже существует,
    # повторно обрабатывать source не нужно.
    if output.exists():
        log(
            f"Output already exists, "
            f"skipping: {output}"
        )
        return

    # ========================================================
    # STABILITY
    # ========================================================

    if not is_stable(video):
        log(
            f"File is not stable yet, "
            f"skipping: {video}"
        )
        return

    # ========================================================
    # INTERNAL TRACKS
    # ========================================================

    try:
        (
            audio_tracks,
            subtitle_tracks,
        ) = find_internal_tracks(
            video
        )

    except ProbeError as exc:
        # КРИТИЧЕСКОЕ ИСПРАВЛЕНИЕ:
        #
        # Ошибка ffprobe НЕ означает:
        #
        #     audio_tracks = []
        #
        # и НЕ должна приводить к поиску external
        # tracks или запуску ffmpeg.
        #
        # Source остаётся нетронутым.
        log(
            f"Cannot inspect source, "
            f"skipping: {video}: {exc}"
        )
        return

    # ========================================================
    # EXTERNAL TRACKS
    # ========================================================
    #
    # Ищем external tracks всегда.
    #
    # Но ниже они будут использованы только если
    # действительно имеют более высокий языковой приоритет,
    # чем уже найденная внутренняя дорожка.
    # ========================================================

    external_audio = select_external_track(
        video,
        AUDIO_EXTENSIONS,
        AUDIO_DIR_NAMES,
    )

    external_subtitle = select_external_track(
        video,
        SUBTITLE_EXTENSIONS,
        SUBTITLE_DIR_NAMES,
    )

    # ========================================================
    # AUDIO PRIORITY
    # ========================================================
    #
    # КРИТИЧЕСКОЕ ИСПРАВЛЕНИЕ:
    #
    # Раньше external audio вообще не искался,
    # если существовала любая internal audio.
    #
    # Теперь:
    #
    #   internal EN + external RU -> external RU
    #   internal JP + external RU -> external RU
    #   internal RU + external EN -> internal RU
    #
    # Используется приоритет:
    #
    #   RU -> JP -> EN
    # ========================================================

    if (
        audio_tracks
        and external_audio
    ):
        internal_priority = LANGUAGE_PRIORITY[
            audio_tracks[0]["language"]
        ]

        external_language = candidate_language(
            external_audio
        )

        if external_language is None:
            external_audio = None

        elif (
            LANGUAGE_PRIORITY[external_language]
            >= internal_priority
        ):
            # Internal track имеет такой же
            # или более высокий приоритет.
            external_audio = None

    # Если internal audio отсутствует,
    # external_audio остаётся выбранным.

    # ========================================================
    # SUBTITLE PRIORITY
    # ========================================================
    #
    # Аналогичная логика для субтитров.
    # ========================================================

    if (
        subtitle_tracks
        and external_subtitle
    ):
        internal_priority = LANGUAGE_PRIORITY[
            subtitle_tracks[0]["language"]
        ]

        external_language = candidate_language(
            external_subtitle
        )

        if external_language is None:
            external_subtitle = None

        elif (
            LANGUAGE_PRIORITY[external_language]
            >= internal_priority
        ):
            external_subtitle = None

    # ========================================================
    # ИНФОРМАЦИЯ О ФАЙЛЕ
    # ========================================================

    log(
        f"Processing: {video}"
    )

    log(
        f"Output: {output}"
    )

    if audio_tracks:
        log(
            "Internal audio: "
            + ", ".join(
                f"{track['language']}:"
                f"{track['title'] or 'untitled'}"
                for track in audio_tracks
            )
        )

    if subtitle_tracks:
        log(
            "Internal subtitles: "
            + ", ".join(
                f"{track['language']}:"
                f"{track['title'] or 'untitled'}"
                for track in subtitle_tracks
            )
        )

    if external_audio:
        log(
            f"External audio: "
            f"{external_audio}"
        )

    if external_subtitle:
        log(
            f"External subtitle: "
            f"{external_subtitle}"
        )

    # ========================================================
    # TEMPORARY OUTPUT
    # ========================================================

    tmp_output = output.with_name(
        output.name + ".processing"
    )

    # ========================================================
    # FFMPEG COMMAND
    # ========================================================
    #
    # КРИТИЧЕСКОЕ ИСПРАВЛЕНИЕ:
    #
    # build_ffmpeg_command() больше не вызывает ffprobe.
    #
    # Поэтому в рамках одного source:
    #
    #     ffprobe -> один раз
    #     ffmpeg  -> один раз
    #
    # вместо:
    #
    #     ffprobe -> find_internal_tracks()
    #     ffprobe -> build_ffmpeg_command()
    # ========================================================

    command = build_ffmpeg_command(
        video,
        tmp_output,
        audio_tracks,
        subtitle_tracks,
        external_audio,
        external_subtitle,
    )

    # ========================================================
    # DRY-RUN
    # ========================================================

    if dry_run:
        log(
            "DRY-RUN: ffmpeg command:"
        )

        log(
            "DRY-RUN: "
            + " ".join(command)
        )

        return

    # ========================================================
    # СОЗДАНИЕ OUTPUT КАТАЛОГА
    # ========================================================

    output.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    # ========================================================
    # СТАРЫЙ .processing
    # ========================================================

    if tmp_output.exists():

        if not remove_file_safely(
            tmp_output,
            "stale processing file",
        ):
            return

    # ========================================================
    # FFMPEG
    # ========================================================

    try:
        result = run_command(
            command,
            check=False,
        )

        # ====================================================
        # FFMPEG ERROR
        # ====================================================

        if result.returncode != 0:
            log(
                f"ffmpeg failed for {video}\n"
                f"{result.stderr}"
            )

            remove_file_safely(
                tmp_output,
                "temporary output",
            )

            return

        # ====================================================
        # ПРОВЕРКА TEMPORARY OUTPUT
        # ====================================================

        if not validate_output(
            tmp_output
        ):
            log(
                f"Invalid output: "
                f"{tmp_output}"
            )

            remove_file_safely(
                tmp_output,
                "invalid temporary output",
            )

            return

        # ====================================================
        # ATOMIC RENAME
        # ====================================================

        os.replace(
            tmp_output,
            output,
        )

        # ====================================================
        # ПОВТОРНАЯ ПРОВЕРКА FINAL OUTPUT
        # ====================================================
        #
        # КРИТИЧЕСКОЕ ИСПРАВЛЕНИЕ:
        #
        # Если final output оказался повреждённым,
        # его обязательно удаляем.
        #
        # Иначе следующая итерация увидит:
        #
        #     output.exists() == True
        #
        # и навсегда пропустит source.
        # ========================================================

        if not validate_output(
            output
        ):
            log(
                f"Final validation failed: "
                f"{output}"
            )

            # Нельзя оставлять повреждённый
            # final output.
            remove_file_safely(
                output,
                "invalid final output",
            )

            # Source НЕ удаляется.
            # Следующий scan сможет повторить
            # конвертацию.
            return

        # ====================================================
        # ПОВТОРНАЯ ПРОВЕРКА SOURCE
        # ====================================================
        #
        # Между первым is_stable() и завершением ffmpeg
        # source мог измениться.
        #
        # Перед удалением source убеждаемся, что он
        # всё ещё стабилен.
        # ========================================================

        if not is_stable(
            video
        ):
            log(
                "Source changed or is no longer "
                "stable after conversion; "
                f"keeping source: {video}"
            )

            # Output уже валиден и остаётся.
            # Source остаётся для безопасности.
            return

        # ====================================================
        # УДАЛЕНИЕ SOURCE
        # ====================================================

        video.unlink()

        log(
            f"Completed: {output}"
        )

        log(
            f"Source deleted: {video}"
        )

    except Exception as exc:
        # Любая неожиданная ошибка НЕ должна
        # приводить к удалению source.

        log(
            f"Processing exception "
            f"for {video}: {exc}"
        )

        # Удаляем только временный output.
        remove_file_safely(
            tmp_output,
            "temporary output",
        )


# ============================================================
# СКАНИРОВАНИЕ COMPLETE
# ============================================================

def scan(
    dry_run: bool = False,
) -> None:
    """
    Выполняет один проход по каталогу complete.

    Symlink-файлы намеренно пропускаются.
    """

    if not COMPLETE.exists():
        log(
            f"Complete directory does not exist: "
            f"{COMPLETE}"
        )
        return

    # rglob работает только внутри COMPLETE.
    #
    # transmission/incomplete здесь вообще
    # не сканируется.
    for path in COMPLETE.rglob("*"):

        # ====================================================
        # SYMLINK PROTECTION
        # ====================================================
        #
        # Path.is_file() следует за symlink.
        #
        # Поэтому проверка is_symlink() должна идти
        # ДО is_file().
        #
        if path.is_symlink():
            continue

        if not path.is_file():
            continue

        if (
            path.suffix.lower()
            not in VIDEO_EXTENSIONS
        ):
            continue

        try:
            process_video(
                path,
                dry_run=dry_run,
            )

        except Exception as exc:
            # Ошибка одного файла не должна
            # останавливать обработку остальных.

            log(
                f"Unhandled error for "
                f"{path}: {exc}"
            )


# ============================================================
# БЛОКИРОВКА ЭКЗЕМПЛЯРА
# ============================================================

def acquire_lock():
    """
    Создаёт эксклюзивную flock-блокировку.

    Возвращает открытый file handle.

    ВАЖНО:

        flock привязан к открытому descriptor.

        Поэтому вызывающий код обязан сохранить
        возвращённый handle живым до завершения процесса.
    """

    LOCK_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    lock_handle = open(
        LOCK_FILE,
        "w",
    )

    try:
        fcntl.flock(
            lock_handle,
            fcntl.LOCK_EX | fcntl.LOCK_NB,
        )

    except BlockingIOError:
        log(
            "Another anime-converter "
            "instance is already running."
        )

        lock_handle.close()

        sys.exit(1)

    except Exception:
        lock_handle.close()
        raise

    return lock_handle


# ============================================================
# ARGUMENTS
# ============================================================

def parse_arguments() -> argparse.Namespace:
    """
    Разбирает аргументы командной строки.

    Поддерживается:

        --dry-run
    """

    parser = argparse.ArgumentParser(
        description="Anime converter"
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "scan once and show planned "
            "ffmpeg commands without "
            "modifying files"
        ),
    )

    return parser.parse_args()


# ============================================================
# MAIN
# ============================================================

def main() -> None:
    """
    Главная функция программы.

    Обычный режим:

        - получает flock;
        - сохраняет file handle;
        - создаёт output;
        - запускает daemon loop.

    Dry-run:

        - lock не требуется;
        - выполняется один scan;
        - программа завершается.
    """

    args = parse_arguments()

    lock_handle = None

    # ========================================================
    # LOCK
    # ========================================================

    if not args.dry_run:
        lock_handle = acquire_lock()

    log(
        "anime-converter started"
    )

    if args.dry_run:
        log(
            "DRY-RUN mode enabled: "
            "no files will be changed"
        )

    log(
        f"Source: {COMPLETE}"
    )

    log(
        f"Output: {ONGOING}"
    )

    # ========================================================
    # DRY-RUN
    # ========================================================

    if args.dry_run:
        scan(
            dry_run=True
        )
        return

    # ========================================================
    # OUTPUT DIRECTORY
    # ========================================================

    ONGOING.mkdir(
        parents=True,
        exist_ok=True,
    )

    # ========================================================
    # DAEMON LOOP
    # ========================================================
    #
    # Ссылка на lock_handle остаётся живой всё время,
    # пока работает main().
    #
    # Это гарантирует, что flock не будет освобождён
    # преждевременно из-за уничтожения объекта.
    # ========================================================

    _ = lock_handle

    while True:

        try:
            scan()

        except Exception as exc:
            log(
                f"Scan error: {exc}"
            )

        time.sleep(
            SCAN_INTERVAL
        )


# ============================================================
# ТОЧКА ВХОДА
# ============================================================

if __name__ == "__main__":
    main()
