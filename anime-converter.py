#!/usr/bin/env python3

# ============================================================
# ANIME CONVERTER
# ============================================================
#
# Назначение:
#   Автоматически находит готовые видео в:
#
#       /home/chaser/data/jormungandr/transmission/complete
#
#   и конвертирует их в:
#
#       /home/chaser/data/jormungandr/ongoing
#
# Основные задачи:
#   - рекурсивно искать видео;
#   - определять аудио и субтитры;
#   - отдавать приоритет русскому языку;
#   - при необходимости находить внешние аудио/субтитры;
#   - конвертировать видео в H.264;
#   - ограничивать максимальную высоту 720p без апскейла;
#   - конвертировать звук в MP3 192k;
#   - сохранять результат в MKV;
#   - проверять готовый файл;
#   - только после успешной проверки удалять исходник;
#   - не запускать одновременно несколько экземпляров программы;
#   - поддерживать безопасный режим --dry-run.
#
# ВАЖНО:
#   Скрипт работает ТОЛЬКО с transmission/complete.
#   Каталог transmission/incomplete вообще не сканируется.
# ============================================================


import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple


# ============================================================
# ОСНОВНЫЕ ПУТИ
# ============================================================

# Корневой каталог всей медиаструктуры.
ROOT = Path("/home/chaser/data/jormungandr")

# Каталог, куда Transmission складывает полностью скачанные файлы.
#
# Именно этот каталог сканирует конвертер.
COMPLETE = ROOT / "transmission" / "complete"

# Каталог, куда складываются готовые MKV-файлы.
ONGOING = ROOT / "ongoing"

# Файл блокировки.
#
# Он нужен, чтобы одновременно не работали два экземпляра
# anime-converter.
LOCK_FILE = ROOT / ".anime-converter.lock"


# ============================================================
# ПАРАМЕТРЫ РАБОТЫ
# ============================================================

# Как часто повторять сканирование каталога.
SCAN_INTERVAL = 30

# Сколько секунд подождать и проверить размер файла повторно.
#
# Это позволяет убедиться, что файл действительно перестал
# изменяться и Transmission уже закончил запись.
STABILITY_DELAY = 15

# Минимальный возраст файла в секундах.
#
# Даже если размер файла не изменяется, совсем свежие файлы
# не обрабатываются сразу.
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

# Внешний audio разрешается искать в таких каталогах,
# но только если они являются непосредственными дочерними
# каталогами каталога, содержащего видео.
#
# Поиск по родительским каталогам выше каталога видео запрещён.
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
#     RU → JP → EN
#
LANGUAGE_PRIORITY = {
    "ru": 0,
    "jp": 1,
    "en": 2,
}


# ============================================================
# ЛОГИРОВАНИЕ
# ============================================================

def log(message: str) -> None:
    """
    Печатает сообщение с текущей датой и временем.

    Сообщения видны в journalctl, если программа запущена
    через systemd.
    """

    print(
        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}",
        flush=True,
    )


# ============================================================
# ЗАПУСК ВНЕШНИХ КОМАНД
# ============================================================

def run_command(
    command: List[str],
    check: bool = True,
) -> subprocess.CompletedProcess:
    """
    Запускает внешнюю программу.

    Основные внешние программы:

        ffprobe
        ffmpeg

    stdout и stderr сохраняются, чтобы программа могла
    обработать результат и вывести ошибки в журнал.
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

def ffprobe_json(path: Path) -> dict:
    """
    Получает техническую информацию о файле через ffprobe.

    В результате получаем JSON с информацией о:

        - видео;
        - аудио;
        - субтитрах;
        - codec;
        - language;
        - title;
        - размерах видео.
    """

    result = run_command([
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        "-show_format",
        str(path),
    ])

    return json.loads(result.stdout)


# ============================================================
# НОРМАЛИЗАЦИЯ ТЕКСТА
# ============================================================

def normalize_text(value: str) -> str:
    """
    Приводит строку к удобному для сравнения виду.

    Например:

        Episode_01-RUS.mkv

    превращается примерно в:

        episode 01 rus.mkv
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

def stream_language(stream: dict) -> Optional[str]:
    """
    Определяет язык внутреннего аудио/субтитров.

    В первую очередь используются:

        tags.language
        tags.title

    Если язык не найден, проверяется codec_name.
    """

    tags = stream.get("tags", {})

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
) -> Tuple[List[dict], List[dict]]:
    """
    Ищет внутри исходного видео:

        - аудиодорожки;
        - субтитры.

    Используются только:

        Russian
        Japanese
        English

    Результаты сортируются по приоритету языка.
    """

    try:

        data = ffprobe_json(path)

    except Exception as exc:

        log(
            f"ffprobe failed for {path}: {exc}"
        )

        return [], []


    audio = []
    subtitles = []


    for stream in data.get("streams", []):

        language = stream_language(
            stream
        )

        if language not in LANGUAGE_PRIORITY:
            continue


        item = {
            "index": stream.get("index"),
            "language": language,
            "title": stream.get(
                "tags",
                {},
            ).get(
                "title",
                "",
            ),
        }


        if stream.get("codec_type") == "audio":

            audio.append(item)

        elif stream.get("codec_type") == "subtitle":

            subtitles.append(item)


    audio.sort(
        key=lambda x: LANGUAGE_PRIORITY[x["language"]]
    )

    subtitles.sort(
        key=lambda x: LANGUAGE_PRIORITY[x["language"]]
    )

    return audio, subtitles


# ============================================================
# ПОИСК НОМЕРА ЭПИЗОДА
# ============================================================

def episode_numbers(name: str) -> List[str]:
    """
    Извлекает номера эпизодов из имени.

    Поддерживаются, например:

        Anime - 01
        Anime 01
        Anime - 01-02
        Anime E01

    Возвращается список найденных номеров.
    """

    normalized = normalize_text(name)

    result = []


    # Явные конструкции Episode 01 / Ep 01 / E01.
    explicit_patterns = [
        r"\b(?:episode|ep|e)\s*0*(\d{1,4})\b",
    ]


    for pattern in explicit_patterns:

        for match in re.finditer(
            pattern,
            normalized,
        ):

            result.append(
                match.group(1)
            )


    # Диапазоны 01-02.
    for match in re.finditer(
        r"(?<!\d)(\d{1,4})\s*[-~]\s*(\d{1,4})(?!\d)",
        normalized,
    ):

        result.append(
            match.group(1)
        )

        result.append(
            match.group(2)
        )


    # Если явного Episode/E номера нет,
    # разрешаем обычное число только как fallback.
    #
    # При этом числа вроде года 2024 тоже могут встретиться,
    # поэтому они не являются самостоятельным основанием
    # для выбора внешней дорожки.
    if not result:

        for match in re.finditer(
            r"(?<!\d)(\d{1,3})(?!\d)",
            normalized,
        ):

            value = match.group(1)

            if int(value) <= 999:

                result.append(value)


    # Сохраняем порядок, но убираем дубликаты.
    unique = []

    for value in result:

        if value not in unique:

            unique.append(value)

    return unique


# ============================================================
# НОМЕР СЕЗОНА
# ============================================================

def season_numbers(name: str) -> List[str]:
    """
    Извлекает явные номера сезонов.

    Например:

        S01
        S02
        Season 1

    Если сезон явно указан в одном имени,
    а в другом явно указан другой сезон,
    такие файлы нельзя автоматически сопоставлять.
    """

    normalized = normalize_text(name)

    result = []


    pattern = (
        r"\b(?:s|season)\s*0*(\d{1,3})\b"
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

def normalized_stem(path: Path) -> str:
    """
    Получает упрощённое имя файла для сравнения.

    Убираются:

        - языковые обозначения;
        - содержимое [...] ;
        - содержимое (...) ;
        - лишние спецсимволы.

    Номер эпизода и сезон намеренно НЕ удаляются здесь:
    они дополнительно проверяются отдельными функциями.
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

def series_signature(path: Path) -> str:
    """
    Получает нормализованное имя anime/series.

    Из имени удаляются:

        - язык;
        - Episode/Ep/E + номер;
        - номер сезона;
        - номер эпизода.

    Это критически важно для внешних дорожек.

    Например:

        Anime A - Episode 01.mkv
        Anime B - Episode 01.RUS.mka

    после обработки будут иметь разные series_signature.

    Поэтому совпадение только по "Episode 01" больше
    не позволит автоматически подключить Anime B.
    """

    text = normalized_stem(path)


    # Убираем конструкции Episode 01 / Ep 01 / E01.
    text = re.sub(
        r"\b(?:episode|ep|e)\s*\d{1,4}"
        r"(?:\s*[-~]\s*\d{1,4})?\b",
        " ",
        text,
    )


    # Убираем Season 1 / S01.
    text = re.sub(
        r"\b(?:season|s)\s*\d{1,3}\b",
        " ",
        text,
    )


    # Удаляем отдельные номера эпизодов.
    for episode in episode_numbers(
        path.stem
    ):

        text = re.sub(
            rf"(?<!\d){re.escape(episode)}(?!\d)",
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

    Важный принцип:

        совпадение Episode 01 само по себе НЕ является
        достаточным условием.

    Обязательно учитывается anime/series name.

    Алгоритм намеренно простой и предсказуемый.
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


    score = 0


    # ========================================================
    # SERIES NAME
    # ========================================================

    if video_series == candidate_series:

        # Идеальное совпадение имени серии.
        score += 100


    elif (
        video_series in candidate_series
        or candidate_series in video_series
    ):

        # Одно полное имя содержится в другом.
        score += 75


    else:

        video_words = set(
            video_series.split()
        )

        candidate_words = set(
            candidate_series.split()
        )


        common_words = (
            video_words
            & candidate_words
        )


        # Нет общих слов в имени series —
        # почти наверняка другой anime.
        if not common_words:

            return -1000


        smaller_count = min(
            len(video_words),
            len(candidate_words),
        )


        if smaller_count == 0:

            return -1000


        similarity = (
            len(common_words)
            / smaller_count
        )


        # Требуем достаточно сильного совпадения.
        if similarity < 0.60:

            return -1000


        score += (
            50
            + int(similarity * 20)
        )


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


    # Если сезон есть только у одного файла,
    # это не автоматический отказ, но и бонуса нет.


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


    # Если видео содержит номер эпизода,
    # а кандидат вообще не содержит номера,
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
    directory_names: set,
) -> List[Path]:
    """
    Возвращает безопасные каталоги для поиска внешних дорожек.

    Разрешены только:

        1. каталог самого видео;
        2. специальные каталоги, являющиеся непосредственными
           дочерними каталогами каталога видео.

    Например:

        Anime/
        ├── Episode 01.mkv
        └── audio/
            └── Episode 01.RUS.mka

    разрешено.

    А:

        complete/
        ├── Anime A/
        │   └── Episode 01.mkv
        └── audio/
            └── Episode 01.RUS.mka

    НЕ должно приводить к поиску в complete/audio.

    Это защищает соседние anime/series от ошибочного matching.
    """

    directories = [
        video.parent
    ]


    try:

        for child in video.parent.iterdir():

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
            f"Unable to inspect external-track directories "
            f"near {video}: {exc}"
        )


    return directories


# ============================================================
# ПОИСК ВНЕШНИХ ДОРОЖЕК
# ============================================================

def find_external_tracks(
    video: Path,
    extension_set: set,
    directory_names: set,
) -> List[Tuple[int, Path, Optional[str]]]:
    """
    Ищет внешние audio/subtitle только в безопасном контексте.

    ВАЖНО:

        Родительские каталоги выше video.parent больше
        никогда не сканируются.

    Для каждого кандидата дополнительно проверяются:

        - язык;
        - series name;
        - season;
        - episode;
        - отсутствие противоречий.

    Если score недостаточно высокий,
    файл вообще не возвращается как кандидат.
    """

    candidates = []


    search_dirs = external_search_directories(
        video,
        directory_names,
    )


    seen = set()


    for search_dir in search_dirs:

        try:

            entries = list(
                search_dir.iterdir()
            )

        except OSError:

            continue


        for candidate in entries:

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


            # Если язык неизвестен,
            # автоматически использовать файл нельзя.
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
            #
            # Лучше не подключить дорожку,
            # чем подключить дорожку от другого anime.
            if score < 80:

                continue


            # Специальный каталог даёт только небольшой
            # дополнительный бонус.
            #
            # Он НЕ заменяет matching по series/episode.
            if (
                candidate.parent != video.parent
            ):

                score += 10


            candidates.append(
                (
                    score,
                    candidate,
                    language,
                )
            )


    # Лучшие кандидаты идут первыми.
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
    extension_set: set,
    directory_names: set,
) -> Optional[Path]:
    """
    Выбирает внешний audio/subtitle.

    Если два кандидата имеют одинаковый score,
    но относятся к одному языку, автоматический выбор
    считается неоднозначным и отменяется.

    Это дополнительная защита от случайного подключения
    неправильного файла.
    """

    candidates = find_external_tracks(
        video,
        extension_set,
        directory_names,
    )


    if not candidates:

        return None


    best_score = candidates[0][0]


    # Берём только кандидатов с максимальным score.
    best_candidates = [
        item
        for item in candidates
        if item[0] == best_score
    ]


    # Если несколько файлов одного языка имеют одинаковый
    # максимальный score, автоматический выбор опасен.
    languages = {}

    for item in best_candidates:

        language = item[2]

        languages.setdefault(
            language,
            [],
        ).append(item)


    for language, items in languages.items():

        if len(items) > 1:

            log(
                "Ambiguous external track candidates for "
                f"{video}, language={language}: "
                + ", ".join(
                    str(item[1])
                    for item in items
                )
            )

            # Не подключаем неоднозначный кандидат.
            return None


    best_score, best_path, best_language = (
        candidates[0]
    )


    log(
        f"Selected external track: "
        f"{best_path} "
        f"(language={best_language}, score={best_score})"
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

    Последовательность:

        1. проверить возраст;
        2. запомнить размер;
        3. подождать;
        4. снова проверить размер.

    Если размер не изменился,
    файл считается готовым к обработке.
    """

    try:

        stat = path.stat()

    except OSError:

        return False


    age = (
        time.time()
        - stat.st_mtime
    )


    if age < MIN_FILE_AGE:

        return False


    first_size = stat.st_size


    time.sleep(
        STABILITY_DELAY
    )


    try:

        second_size = path.stat().st_size

    except OSError:

        return False


    return first_size == second_size


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
        - размеры определены;
        - высота <= 720;
        - ширина чётная;
        - присутствует хотя бы один аудиопоток.
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

    except Exception as exc:

        log(
            f"Output validation failed for {path}: {exc}"
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
        if stream.get("codec_type") == "video"
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
            f"Output video codec is not H.264: {path}"
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
            f"Output has invalid video dimensions: {path}"
        )

        return False


    # Апскейла быть не должно:
    # максимальная высота — 720.
    if height > 720:

        log(
            f"Output height is greater than 720: "
            f"{width}x{height}"
        )

        return False


    # Ширина должна быть чётной,
    # потому что scale использует -2.
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
        if stream.get("codec_type") == "audio"
    ]


    if not audio_streams:

        log(
            f"Output has no audio stream: {path}"
        )

        return False


    # Все выходные audio должны быть MP3.
    for stream in audio_streams:

        if stream.get("codec_name") != "mp3":

            log(
                f"Output contains non-MP3 audio: {path}"
            )

            return False


    return True


# ============================================================
# СОЗДАНИЕ КОМАНДЫ FFMPEG
# ============================================================

def build_ffmpeg_command(
    source: Path,
    output: Path,
    external_audio: Optional[Path],
    external_subtitle: Optional[Path],
) -> List[str]:
    """
    Создаёт полный набор параметров ffmpeg.

    Основные настройки:

        Video:
            libx264
            максимум 720p
            без апскейла
            veryfast

        Audio:
            libmp3lame
            192 kbit/s

        Subtitles:
            copy

        Container:
            MKV
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
    # INTERNAL AUDIO / SUBTITLES
    # --------------------------------------------------------

    audio_tracks, subtitle_tracks = (
        find_internal_tracks(source)
    )


    for track in audio_tracks:

        command += [
            "-map",
            f"0:{track['index']}",
        ]


    # Внешний audio input идёт после source,
    # поэтому его индекс всегда 1.
    if external_audio:

        command += [
            "-map",
            "1:a:0",
        ]


    for track in subtitle_tracks:

        command += [
            "-map",
            f"0:{track['index']}",
        ]


    # Если external_audio есть:
    #
    #   source = 0
    #   audio  = 1
    #   subtitle = 2
    #
    # Если external_audio отсутствует:
    #
    #   source = 0
    #   subtitle = 1
    #
    if external_subtitle:

        subtitle_input_index = (
            1 + int(bool(external_audio))
        )

        command += [
            "-map",
            f"{subtitle_input_index}:s:0",
        ]


    # ========================================================
    # VIDEO SCALE
    # ========================================================
    #
    # Ключевая защита от апскейла:
    #
    #     input <= 720p -> исходная высота
    #     input > 720p  -> высота 720
    #
    # -2 автоматически рассчитывает чётную ширину
    # с сохранением исходных пропорций.
    # ========================================================

    command += [
        "-vf",
        "scale='if(gt(ih,720),-2,iw)':'if(gt(ih,720),720,ih)'",

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

    audio_count = (
        len(audio_tracks)
        + int(bool(external_audio))
    )


    if audio_count:

        # Аудиодорожки предварительно отсортированы
        # по приоритету RU -> JP -> EN.
        command += [
            "-disposition:a:0",
            "default",
        ]


    # ========================================================
    # DEFAULT SUBTITLE
    # ========================================================

    subtitle_count = (
        len(subtitle_tracks)
        + int(bool(external_subtitle))
    )


    if subtitle_count:

        command += [
            "-disposition:s:0",
            "default",
        ]


    command += [
        str(output)
    ]


    return command


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
        3. находит дорожки;
        4. создаёт .processing;
        5. запускает ffmpeg;
        6. проверяет .processing;
        7. переименовывает в final;
        8. снова проверяет final;
        9. удаляет исходник.

    В dry-run:

        - ffmpeg НЕ запускается;
        - файлы НЕ создаются;
        - исходник НЕ удаляется;
        - файлы НЕ перемещаются;
        - только показывается предполагаемая команда.
    """

    # ========================================================
    # OUTPUT
    # ========================================================

    output = output_path_for(
        video
    )


    # Если output уже существует,
    # повторно обрабатывать исходник не нужно.
    if output.exists():

        log(
            f"Output already exists, skipping: {output}"
        )

        return


    # Проверяем стабильность исходного файла.
    #
    # Даже dry-run не должен считать совсем свежий файл
    # полностью готовым.
    if not is_stable(video):

        log(
            f"File is not stable yet, skipping: {video}"
        )

        return


    # ========================================================
    # INTERNAL TRACKS
    # ========================================================

    audio_tracks, subtitle_tracks = (
        find_internal_tracks(video)
    )


    external_audio = None
    external_subtitle = None


    # Если подходящих внутренних аудио нет,
    # ищем безопасный внешний источник.
    if not audio_tracks:

        external_audio = select_external_track(
            video,
            AUDIO_EXTENSIONS,
            AUDIO_DIR_NAMES,
        )


    # Если внутренних субтитров нет,
    # ищем безопасный внешний источник.
    if not subtitle_tracks:

        external_subtitle = select_external_track(
            video,
            SUBTITLE_EXTENSIONS,
            SUBTITLE_DIR_NAMES,
        )


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
            f"External audio: {external_audio}"
        )


    if external_subtitle:

        log(
            f"External subtitle: {external_subtitle}"
        )


    # ========================================================
    # DRY-RUN
    # ========================================================
    #
    # В этом режиме мы намеренно не создаём каталог output,
    # временный файл или какой-либо другой объект.
    #
    # Команда ffmpeg только строится и выводится.
    # ========================================================

    if dry_run:

        # Для dry-run можно использовать предполагаемый
        # .processing path — он нигде не создаётся.
        tmp_output = output.with_name(
            output.name + ".processing"
        )


        command = build_ffmpeg_command(
            video,
            tmp_output,
            external_audio,
            external_subtitle,
        )


        log(
            "DRY-RUN: ffmpeg command:"
        )

        log(
            "DRY-RUN: "
            + " ".join(
                command
            )
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
    # ВРЕМЕННЫЙ OUTPUT
    # ========================================================
    #
    # .processing никогда не считается готовым результатом.
    #
    # Если ffmpeg аварийно завершится,
    # исходный файл остаётся нетронутым.
    # ========================================================

    tmp_output = output.with_name(
        output.name + ".processing"
    )


    # Если старый .processing существует,
    # он удаляется только как временный незавершённый output.
    #
    # Исходный video при этом не затрагивается.
    if tmp_output.exists():

        try:

            tmp_output.unlink()

        except OSError as exc:

            log(
                f"Cannot remove stale processing file "
                f"{tmp_output}: {exc}"
            )

            return


    # ========================================================
    # FFMPEG COMMAND
    # ========================================================

    command = build_ffmpeg_command(
        video,
        tmp_output,
        external_audio,
        external_subtitle,
    )


    try:

        # ====================================================
        # FFMPEG
        # ====================================================

        result = run_command(
            command,
            check=False,
        )


        # Любая ошибка ffmpeg означает:
        #
        #   output недействителен;
        #   исходник НЕ удаляем.
        if result.returncode != 0:

            log(
                f"ffmpeg failed for {video}\n"
                f"{result.stderr}"
            )


            try:

                tmp_output.unlink()

            except OSError:

                pass


            return


        # ====================================================
        # ПРОВЕРКА TEMPORARY OUTPUT
        # ====================================================
        #
        # Если ffmpeg сообщил успех, всё равно нельзя
        # доверять exit code как единственной проверке.
        #
        # Проверяем реальный контейнер и streams.
        # ====================================================

        if not validate_output(
            tmp_output
        ):

            log(
                f"Invalid output: {tmp_output}"
            )


            try:

                tmp_output.unlink()

            except OSError:

                pass


            # КРИТИЧЕСКОЕ ПРАВИЛО:
            #
            # Исходник здесь НЕ удаляется.
            return


        # ====================================================
        # ATOMIC RENAME
        # ====================================================
        #
        # Только после успешной проверки temporary output
        # он становится финальным файлом.
        # ====================================================

        os.replace(
            tmp_output,
            output,
        )


        # ====================================================
        # ПОВТОРНАЯ ПРОВЕРКА FINAL OUTPUT
        # ====================================================
        #
        # Проверяем уже тот файл, который будет оставлен
        # пользователю.
        # ====================================================

        if not validate_output(
            output
        ):

            log(
                f"Final validation failed: {output}"
            )


            # Исходник НЕ удаляется.
            #
            # Более того, output остаётся для диагностики,
            # а оригинал продолжает существовать.
            return


        # ====================================================
        # УДАЛЕНИЕ ИСХОДНИКА
        # ====================================================
        #
        # Это единственное место, где исходник удаляется.
        #
        # К этому моменту гарантируется:
        #
        #   1. ffmpeg завершился успешно;
        #   2. temporary output проверен;
        #   3. output перемещён в final;
        #   4. final output повторно проверен;
        #   5. output содержит H.264;
        #   6. высота <= 720;
        #   7. ширина чётная;
        #   8. присутствует MP3 audio.
        #
        # При любой ошибке выше этот код не выполняется.
        # ========================================================

        video.unlink()


        log(
            f"Completed: {output}"
        )

        log(
            f"Source deleted: {video}"
        )


    except Exception as exc:

        # Любая неожиданная ошибка не должна приводить
        # к удалению исходного файла.
        log(
            f"Processing exception for {video}: {exc}"
        )


        # Удаляем только временный незавершённый output.
        try:

            tmp_output.unlink()

        except OSError:

            pass


# ============================================================
# СКАНИРОВАНИЕ COMPLETE
# ============================================================

def scan(
    dry_run: bool = False,
) -> None:
    """
    Выполняет один проход по каталогу complete.

    В dry-run:

        - один scan;
        - команды только показываются;
        - ffmpeg не запускается;
        - файлы не изменяются.
    """

    if not COMPLETE.exists():

        log(
            f"Complete directory does not exist: {COMPLETE}"
        )

        return


    # rglob работает только внутри COMPLETE.
    #
    # transmission/incomplete здесь физически недоступен
    # для сканирования.
    for path in COMPLETE.rglob("*"):

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

            # Ошибка одного файла не должна останавливать
            # обработку остальных.
            log(
                f"Unhandled error for {path}: {exc}"
            )


# ============================================================
# БЛОКИРОВКА ЭКЗЕМПЛЯРА
# ============================================================

def acquire_lock():
    """
    Создаёт эксклюзивную flock-блокировку.

    ВАЖНО:

        fcntl.flock() привязан к открытому file descriptor.

    Поэтому вызывающий код ОБЯЗАН сохранить возвращённый
    file handle живым до завершения процесса.

    Если второй экземпляр уже работает,
    новый экземпляр сразу завершается.
    """

    import fcntl


    # Каталог для lock-файла должен существовать.
    LOCK_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )


    # Открываем lock-файл.
    #
    # Само существование файла НЕ является механизмом блокировки.
    # Важен именно fcntl.flock().
    lock_handle = open(
        LOCK_FILE,
        "w",
    )


    try:

        # Неблокирующая эксклюзивная блокировка.
        fcntl.flock(
            lock_handle,
            fcntl.LOCK_EX | fcntl.LOCK_NB,
        )


    except BlockingIOError:

        # Другой экземпляр уже удерживает flock.
        log(
            "Another anime-converter instance is already running."
        )


        # Закрываем descriptor перед выходом.
        lock_handle.close()


        sys.exit(1)


    except Exception:

        # При другой ошибке также освобождаем descriptor.
        lock_handle.close()

        raise


    # ========================================================
    # КРИТИЧЕСКОЕ ИЗМЕНЕНИЕ
    # ========================================================
    #
    # Возвращаем handle вызывающему коду.
    #
    # main() обязан сохранить эту ссылку.
    #
    # Пока handle жив, flock продолжает действовать.
    # ========================================================

    return lock_handle


# ============================================================
# ARGUMENTS
# ============================================================

def parse_arguments() -> argparse.Namespace:
    """
    Разбирает аргументы командной строки.

    Поддерживается:

        --dry-run

    Dry-run специально сделан одноразовым:
    это позволяет безопасно проверить текущее содержимое
    complete без запуска daemon loop.
    """

    parser = argparse.ArgumentParser(
        description="Anime converter"
    )


    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "scan once and show planned ffmpeg commands "
            "without modifying files"
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


    # ========================================================
    # LOCK
    # ========================================================
    #
    # В dry-run lock не нужен, потому что этот режим:
    #
    #   - не запускает ffmpeg;
    #   - не создаёт output;
    #   - не удаляет исходники;
    #   - не перемещает файлы.
    #
    # В обычном режиме handle сохраняется в локальной переменной
    # main() на весь срок жизни процесса.
    # ========================================================

    lock_handle = None


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
    #
    # Выполняем ровно один scan и завершаемся.
    #
    # Каталог ONGOING специально не создаём.
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
