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
#   - конвертировать видео в H.264 720p;
#   - конвертировать звук в MP3;
#   - сохранять результат в MKV;
#   - проверять готовый файл;
#   - только после успешной проверки удалять исходник;
#   - не запускать одновременно несколько экземпляров программы.
#
# ВАЖНО:
#   Скрипт работает ТОЛЬКО с transmission/complete.
#   Каталог transmission/incomplete вообще не сканируется.
# ============================================================


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

# Видео, которые программа умеет находить.
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


# Поддерживаемые внешние субтитры.
SUBTITLE_EXTENSIONS = {
    ".ass",
    ".ssa",
    ".srt",
    ".vtt",
    ".sup",
    ".sub",
}


# Поддерживаемые внешние аудиофайлы.
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
# НАЗВАНИЯ КАТАЛОГОВ С ВНЕШНИМИ ДОРОЖКАМИ
# ============================================================

# Если внешний звук лежит, например, в:
#
#   Anime/audio/
#   Anime/озвучка/
#
# программа считает это дополнительным признаком того,
# что файл действительно является аудиодорожкой для серии.
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


# Аналогично для субтитров.
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

# Возможные обозначения языков в именах файлов и metadata.
#
# Например:
#
#   Episode.01.RUS.mkv
#   Episode.01.JPN.mka
#   Episode.01.ENG.ass
#
# будут распознаны соответственно как русский,
# японский и английский.
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
# Русский будет выбран первым.
# Если русского нет — японский.
# Если японского нет — английский.
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

    Основные внешние программы, которые использует скрипт:

        ffprobe
        ffmpeg

    stdout и stderr сохраняются в памяти, чтобы программа могла
    обработать результат и записать ошибки в лог.
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
    Получает техническую информацию о видео через ffprobe.

    В результате получаем JSON с информацией о:

        - видео;
        - аудио;
        - субтитрах;
        - codec;
        - language;
        - title;
        - размерах видео и т.д.
    """

    result = run_command([
        "ffprobe",

        # Не показывать лишний служебный вывод.
        "-v", "error",

        # Вернуть результат в JSON.
        "-print_format", "json",

        # Получить информацию о потоках.
        "-show_streams",

        # Получить информацию о контейнере.
        "-show_format",

        str(path),
    ])

    return json.loads(result.stdout)


# ============================================================
# НОРМАЛИЗАЦИЯ ТЕКСТА
# ============================================================

def normalize_text(value: str) -> str:
    """
    Приводит строку к более удобному для сравнения виду.

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

    # Проверяем каждый известный язык.
    for language, tags in LANGUAGES.items():

        # Проверяем все возможные обозначения этого языка.
        for tag in tags:

            # Используем границы слова, чтобы, например,
            # "eng" не совпал случайно с частью другого слова.
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

    В первую очередь смотрим metadata:

        tags.language
        tags.title

    Если язык там не указан, дополнительно проверяем codec_name.
    """

    tags = stream.get("tags", {})

    candidates = [
        tags.get("language", ""),
        tags.get("title", ""),
        stream.get("codec_name", ""),
    ]

    for value in candidates:

        language = detect_language(str(value))

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

    Нас интересуют только:

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


    # Перебираем все streams, которые нашёл ffprobe.
    for stream in data.get("streams", []):

        language = stream_language(stream)

        # Если язык неизвестен или не входит в список
        # нужных языков — эту дорожку не используем.
        if language not in LANGUAGE_PRIORITY:
            continue


        item = {
            "index": stream.get("index"),
            "language": language,
            "title": stream.get("tags", {}).get(
                "title",
                "",
            ),
        }


        # Если это аудио — добавляем в список аудио.
        if stream.get("codec_type") == "audio":

            audio.append(item)


        # Если это субтитры — добавляем в список субтитров.
        elif stream.get("codec_type") == "subtitle":

            subtitles.append(item)


    # Сортировка:
    #
    #   RU
    #   JP
    #   EN
    #
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
    Пытается найти номера эпизодов в имени файла.

    Например:

        Anime - 01.mkv
        Anime 01.mkv
        Anime - 01-02.mkv

    используются для дополнительного сравнения файлов.

    Это особенно важно при поиске внешнего аудио/субтитров.
    """

    patterns = [

        # Диапазон вроде 01-02.
        r"(?<!\d)(\d{1,4})\s*[-~]\s*\d{1,4}(?!\d)",

        # Обычный номер эпизода.
        r"(?<!\d)(\d{1,3})(?:v\d)?(?!\d)",
    ]


    result = []


    for pattern in patterns:

        for match in re.finditer(
            pattern,
            name,
        ):
            result.append(match.group(1))


    return result


# ============================================================
# НОРМАЛИЗОВАННОЕ ИМЯ ФАЙЛА
# ============================================================

def normalized_stem(path: Path) -> str:
    """
    Получает упрощённое имя файла для сравнения.

    Убираются:

        - языковые обозначения;
        - содержимое [...] ;
        - содержимое (...) ;
        - спецсимволы.

    Это помогает сопоставлять:

        Anime.01.mkv

    с:

        Anime.01.RUS.mka
    """

    text = normalize_text(path.stem)


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


    return " ".join(text.split())


# ============================================================
# ОЦЕНКА СХОЖЕСТИ ФАЙЛОВ
# ============================================================

def similarity_score(
    video: Path,
    candidate: Path,
) -> int:
    """
    Оценивает вероятность того, что внешний файл относится
    именно к данному видео.

    Используются:

        - сходство имён;
        - совпадение слов;
        - совпадение номера эпизода;
        - дополнительные признаки каталога.

    Чем выше score — тем вероятнее соответствие.
    """

    video_name = normalized_stem(video)
    candidate_name = normalized_stem(candidate)

    score = 0


    # Полное совпадение нормализованных имён.
    if video_name and candidate_name:

        if video_name == candidate_name:

            score += 100


        # Одно имя содержится внутри другого.
        elif (
            video_name in candidate_name
            or candidate_name in video_name
        ):

            score += 60


        # Считаем совпадающие слова.
        else:

            video_words = set(
                video_name.split()
            )

            candidate_words = set(
                candidate_name.split()
            )

            score += (
                len(video_words & candidate_words)
                * 10
            )


    # Дополнительно сравниваем номера эпизодов.
    video_eps = set(
        episode_numbers(video.stem)
    )

    candidate_eps = set(
        episode_numbers(candidate.stem)
    )


    if video_eps and candidate_eps:

        # Совпадает номер эпизода.
        if video_eps & candidate_eps:

            score += 50

        # Номера есть, но они разные.
        else:

            score -= 100


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

    return detect_language(path.name)


# ============================================================
# ПОИСК ВНЕШНИХ ДОРОЖЕК
# ============================================================

def find_external_tracks(
    video: Path,
    extension_set: set,
    directory_names: set,
) -> List[Tuple[int, Path, Optional[str]]]:
    """
    Ищет внешние аудио или субтитры.

    Поиск происходит:

        - в каталоге самого видео;
        - выше по дереву каталогов.

    Каждый найденный файл получает score.
    """

    candidates = []


    # Начинаем с каталога видео и постепенно идём выше.
    for parent in [
        video.parent
    ] + list(video.parents):

        try:
            entries = list(parent.iterdir())

        except OSError:

            continue


        # Перебираем файлы в текущем каталоге.
        for candidate in entries:

            # Нам нужны только обычные файлы.
            if not candidate.is_file():
                continue


            # Проверяем расширение.
            if candidate.suffix.lower() not in extension_set:
                continue


            # Сам исходный видеофайл исключаем.
            if candidate == video:
                continue


            # Определяем язык внешнего файла.
            language = candidate_language(candidate)


            # Если язык неизвестен или не нужен —
            # внешний файл не используем.
            if language not in LANGUAGE_PRIORITY:
                continue


            # Вычисляем базовую схожесть имени.
            score = similarity_score(
                video,
                candidate,
            )


            # Если файл находится в специальном каталоге
            # audio/subtitles/etc., добавляем дополнительные баллы.
            if candidate.parent.name.lower() in directory_names:

                score += 30


            # Добавляем только потенциально подходящие кандидаты.
            if score > 0:

                candidates.append(
                    (
                        score,
                        candidate,
                        language,
                    )
                )


    # Сначала самые похожие файлы,
    # затем язык по приоритету.
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
    Выбирает лучший внешний audio/subtitle файл.

    Если соответствие слишком слабое, возвращается None.

    Это сделано специально, чтобы программа не добавляла
    случайный аудиофайл или субтитры от другой серии.
    """

    candidates = find_external_tracks(
        video,
        extension_set,
        directory_names,
    )


    if not candidates:
        return None


    best_score, best_path, _ = candidates[0]


    # Минимальный порог доверия.
    #
    # Если score меньше 50, считаем совпадение ненадёжным.
    if best_score < 50:
        return None


    return best_path


# ============================================================
# ПРОВЕРКА СТАБИЛЬНОСТИ ФАЙЛА
# ============================================================

def is_stable(path: Path) -> bool:
    """
    Проверяет, закончил ли Transmission записывать файл.

    Алгоритм:

        1. Запоминаем размер.
        2. Ждём STABILITY_DELAY секунд.
        3. Снова смотрим размер.
        4. Если размер изменился — файл ещё скачивается.
        5. Проверяем возраст файла.
    """

    try:

        stat1 = path.stat()

        time.sleep(
            STABILITY_DELAY
        )

        stat2 = path.stat()

    except OSError:

        return False


    # Размер изменился — файл ещё записывается.
    if stat1.st_size != stat2.st_size:

        return False


    # Проверяем, что файл достаточно старый.
    age = time.time() - stat2.st_mtime

    return age >= MIN_FILE_AGE


# ============================================================
# ФОРМИРОВАНИЕ ПУТИ РЕЗУЛЬТАТА
# ============================================================

def output_path_for(
    video: Path,
) -> Path:
    """
    Строит путь выходного файла.

    Относительная структура внутри complete сохраняется.

    Например:

        complete/Anime/S01/E01.mp4

    станет:

        ongoing/Anime/S01/E01.mkv
    """

    relative = video.relative_to(
        COMPLETE
    )

    return ONGOING / relative.with_suffix(
        ".mkv"
    )


# ============================================================
# ПРОВЕРКА ГОТОВОГО ФАЙЛА
# ============================================================

def validate_output(
    path: Path,
) -> bool:
    """
    Проверяет, что результат действительно является
    корректным видео.

    Проверяется:

        - ffprobe может открыть файл;
        - существует видеопоток;
        - codec = H.264;
        - высота видео не больше 720.
    """

    try:

        data = ffprobe_json(path)

    except Exception as exc:

        log(
            f"Output validation failed for {path}: {exc}"
        )

        return False


    streams = data.get(
        "streams",
        []
    )


    # Ищем видеопотоки.
    video_streams = [
        s
        for s in streams
        if s.get("codec_type") == "video"
    ]


    # Если видео нет — результат некорректен.
    if not video_streams:

        return False


    video_stream = video_streams[0]


    # Проверяем H.264.
    if video_stream.get("codec_name") != "h264":

        return False


    width = video_stream.get(
        "width"
    )

    height = video_stream.get(
        "height"
    )


    if not width or not height:

        return False


    # Высота должна быть максимум 720.
    if height > 720:

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
    Создаёт полный набор параметров для ffmpeg.

    Основные настройки:

        Video:
            libx264
            720p
            veryfast

        Audio:
            libmp3lame
            192 kbit/s

        Subtitles:
            copy

        Container:
            определяется расширением .mkv
    """


    # Базовая команда ffmpeg.
    command = [
        "ffmpeg",

        # Не показывать баннер ffmpeg.
        "-hide_banner",

        # Показывать только предупреждения и ошибки.
        "-loglevel",
        "warning",

        # Перезаписывать временный файл.
        "-y",

        # Основной источник.
        "-i",
        str(source),
    ]


    # Если найден внешний звук,
    # добавляем его как дополнительный input.
    if external_audio:

        command += [
            "-i",
            str(external_audio),
        ]


    # Если найдены внешние субтитры,
    # добавляем их как дополнительный input.
    if external_subtitle:

        command += [
            "-i",
            str(external_subtitle),
        ]


    # --------------------------------------------------------
    # VIDEO
    # --------------------------------------------------------

    # Берём первый видеопоток исходника.
    command += [
        "-map",
        "0:v:0",
    ]


    # --------------------------------------------------------
    # INTERNAL AUDIO / SUBTITLES
    # --------------------------------------------------------

    audio_tracks, subtitle_tracks = find_internal_tracks(
        source
    )


    # Добавляем подходящие внутренние аудиодорожки.
    for track in audio_tracks:

        command += [
            "-map",
            f"0:{track['index']}",
        ]


    # Добавляем внешний звук, если он найден.
    if external_audio:

        command += [
            "-map",
            "1:a:0",
        ]


    # Добавляем внутренние субтитры.
    for track in subtitle_tracks:

        command += [
            "-map",
            f"0:{track['index']}",
        ]


    # Если внешний звук присутствует,
    # индекс внешних субтитров будет 2.
    #
    # Если внешнего звука нет,
    # внешний subtitle input будет 1.
    if external_subtitle:

        subtitle_input_index = (
            1 + bool(external_audio)
        )

        command += [
            "-map",
            f"{subtitle_input_index}:s:0",
        ]


    # --------------------------------------------------------
    # КОДИРОВАНИЕ
    # --------------------------------------------------------

    command += [

        # Масштабирование до 720p.
        #
        # -2 означает:
        #   ширина вычисляется автоматически,
        #   сохраняя пропорции и делая её чётной.
        "-vf",
        "scale=-2:720",

        # H.264.
        "-c:v",
        "libx264",

        # Быстрый preset.
        "-preset",
        "veryfast",

        # MP3 для всех аудиодорожек.
        "-c:a",
        "libmp3lame",

        # Битрейт аудио.
        "-b:a",
        "192k",

        # Субтитры не перекодируем.
        "-c:s",
        "copy",

        # Сохраняем metadata исходника.
        "-map_metadata",
        "0",

        # Сохраняем главы.
        "-map_chapters",
        "0",
    ]


    # --------------------------------------------------------
    # DEFAULT AUDIO
    # --------------------------------------------------------

    # Количество аудиодорожек:
    #
    #   внутренние + внешний файл, если есть.
    audio_count = (
        len(audio_tracks)
        + (1 if external_audio else 0)
    )


    if audio_count:

        # Первой дорожке даём default.
        #
        # Поскольку дорожки предварительно отсортированы
        # по языку, при наличии русского он будет первым.
        command += [
            "-disposition:a:0",
            "default",
        ]


    # --------------------------------------------------------
    # DEFAULT SUBTITLE
    # --------------------------------------------------------

    subtitle_count = (
        len(subtitle_tracks)
        + (1 if external_subtitle else 0)
    )


    if subtitle_count:

        # Первые субтитры помечаем default.
        command += [
            "-disposition:s:0",
            "default",
        ]


    # Последний аргумент — имя выходного файла.
    command += [
        str(output)
    ]


    return command


# ============================================================
# ОБРАБОТКА ОДНОГО ВИДЕО
# ============================================================

def process_video(
    video: Path,
) -> None:
    """
    Полный цикл обработки одного видео.

    Последовательность:

        1. Определить выходной путь.
        2. Проверить, не существует ли результат.
        3. Проверить стабильность исходника.
        4. Найти внутренние дорожки.
        5. При необходимости найти внешние дорожки.
        6. Создать временный output.
        7. Запустить ffmpeg.
        8. Проверить результат.
        9. Переместить его в финальное имя.
        10. Только после этого удалить исходник.
    """


    # Вычисляем путь готового файла.
    output = output_path_for(
        video
    )


    # Создаём каталог результата, если его ещё нет.
    output.parent.mkdir(
        parents=True,
        exist_ok=True,
    )


    # Если готовый файл уже существует,
    # повторно его не обрабатываем.
    if output.exists():

        log(
            f"Output already exists, skipping: {output}"
        )

        return


    # Проверяем, что Transmission действительно
    # закончил работу с файлом.
    if not is_stable(video):

        log(
            f"File is not stable yet, skipping: {video}"
        )

        return


    # Ищем внутренние аудио и субтитры.
    audio_tracks, subtitle_tracks = find_internal_tracks(
        video
    )


    # По умолчанию внешних дорожек нет.
    external_audio = None
    external_subtitle = None


    # Если подходящих внутренних аудиодорожек нет,
    # пытаемся найти внешний аудиофайл.
    if not audio_tracks:

        external_audio = select_external_track(
            video,
            AUDIO_EXTENSIONS,
            AUDIO_DIR_NAMES,
        )


    # Если подходящих внутренних субтитров нет,
    # пытаемся найти внешний subtitle-файл.
    if not subtitle_tracks:

        external_subtitle = select_external_track(
            video,
            SUBTITLE_EXTENSIONS,
            SUBTITLE_DIR_NAMES,
        )


    # Пишем в журнал информацию о предстоящей обработке.
    log(
        f"Processing: {video}"
    )

    log(
        f"Output: {output}"
    )


    # --------------------------------------------------------
    # ЛОГИРОВАНИЕ ВНУТРЕННИХ AUDIO
    # --------------------------------------------------------

    if audio_tracks:

        log(
            "Internal audio: "
            + ", ".join(
                f"{track['language']}:{track['title'] or 'untitled'}"
                for track in audio_tracks
            )
        )


    # --------------------------------------------------------
    # ЛОГИРОВАНИЕ ВНУТРЕННИХ SUBTITLE
    # --------------------------------------------------------

    if subtitle_tracks:

        log(
            "Internal subtitles: "
            + ", ".join(
                f"{track['language']}:{track['title'] or 'untitled'}"
                for track in subtitle_tracks
            )
        )


    # --------------------------------------------------------
    # ЛОГИРОВАНИЕ ВНЕШНЕГО AUDIO
    # --------------------------------------------------------

    if external_audio:

        log(
            f"External audio: {external_audio}"
        )


    # --------------------------------------------------------
    # ЛОГИРОВАНИЕ ВНЕШНИХ SUBTITLE
    # --------------------------------------------------------

    if external_subtitle:

        log(
            f"External subtitle: {external_subtitle}"
        )


    # ========================================================
    # ВРЕМЕННЫЙ ФАЙЛ
    # ========================================================
    #
    # Например:
    #
    #   Episode 01.mkv.processing
    #
    # До успешного завершения такой файл не считается готовым.
    # ========================================================

    tmp_output = output.with_name(
        output.name + ".processing"
    )


    # Если после предыдущего аварийного запуска остался
    # старый временный файл — удаляем его.
    if tmp_output.exists():

        try:

            tmp_output.unlink()

        except OSError:

            pass


    # Создаём команду ffmpeg.
    command = build_ffmpeg_command(
        video,
        tmp_output,
        external_audio,
        external_subtitle,
    )


    try:

        # Запускаем ffmpeg.
        #
        # check=False нужен для того, чтобы самим обработать
        # ошибку и удалить временный файл.
        result = run_command(
            command,
            check=False,
        )


        # ffmpeg завершился с ошибкой.
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
        # ПРОВЕРКА ВРЕМЕННОГО ФАЙЛА
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


            return


        # ====================================================
        # ПЕРЕМЕЩЕНИЕ В ФИНАЛЬНЫЙ ФАЙЛ
        # ====================================================
        #
        # Только теперь .processing превращается
        # в настоящий готовый output.
        # ====================================================

        os.replace(
            tmp_output,
            output,
        )


        # Повторно проверяем уже финальный файл.
        if not validate_output(
            output
        ):

            log(
                f"Final validation failed: {output}"
            )

            return


        # ====================================================
        # УДАЛЕНИЕ ИСХОДНИКА
        # ====================================================
        #
        # Это критически важный момент.
        #
        # Исходный файл удаляется только после:
        #
        #   - успешного ffmpeg;
        #   - проверки временного output;
        #   - перемещения output;
        #   - повторной проверки финального файла.
        # ====================================================

        video.unlink()


        log(
            f"Completed: {output}"
        )

        log(
            f"Source deleted: {video}"
        )


    except Exception as exc:

        # Любая неожиданная ошибка не должна
        # приводить к удалению исходника.
        log(
            f"Processing exception for {video}: {exc}"
        )


        # Пытаемся удалить недоделанный output.
        try:

            tmp_output.unlink()

        except OSError:

            pass


# ============================================================
# СКАНИРОВАНИЕ COMPLETE
# ============================================================

def scan() -> None:
    """
    Один проход по каталогу complete.

    ВАЖНО:

        rglob("*") работает только внутри COMPLETE.

    Поэтому каталог:

        transmission/incomplete

    вообще не затрагивается.
    """

    # Если complete отсутствует,
    # просто записываем это в лог.
    if not COMPLETE.exists():

        log(
            f"Complete directory does not exist: {COMPLETE}"
        )

        return


    # Рекурсивно перебираем всё дерево complete.
    for path in COMPLETE.rglob("*"):

        # Интересуют только файлы.
        if not path.is_file():

            continue


        # Проверяем расширение.
        if path.suffix.lower() not in VIDEO_EXTENSIONS:

            continue


        # Каждый подходящий файл обрабатывается отдельно.
        try:

            process_video(
                path
            )

        except Exception as exc:

            # Ошибка одного файла не должна остановить
            # обработку всей коллекции.
            log(
                f"Unhandled error for {path}: {exc}"
            )


# ============================================================
# БЛОКИРОВКА ЭКЗЕМПЛЯРА
# ============================================================

def acquire_lock():
    """
    Создаёт эксклюзивную flock-блокировку.

    Если второй экземпляр программы уже запущен,
    он сразу завершится.

    Это защищает от ситуации:

        worker #1 -> обрабатывает Episode 01
        worker #2 -> одновременно обрабатывает Episode 01
    """

    import fcntl


    # Убеждаемся, что родительский каталог существует.
    LOCK_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )


    # Открываем lock-файл.
    lock_handle = open(
        LOCK_FILE,
        "w",
    )


    try:

        # Пытаемся получить неблокирующую эксклюзивную блокировку.
        fcntl.flock(
            lock_handle,
            fcntl.LOCK_EX | fcntl.LOCK_NB,
        )


    except BlockingIOError:

        # Другой экземпляр уже работает.
        log(
            "Another anime-converter instance is already running."
        )

        sys.exit(1)


    # Важно вернуть открытый handle.
    #
    # Пока handle жив, flock продолжает действовать.
    return lock_handle


# ============================================================
# MAIN
# ============================================================

def main() -> None:
    """
    Главная функция программы.

    Здесь:

        - устанавливается lock;
        - создаётся каталог output;
        - запускается бесконечный цикл;
        - каждые SCAN_INTERVAL секунд выполняется сканирование.
    """


    # Не позволяем запускать два worker одновременно.
    acquire_lock()


    log(
        "anime-converter started"
    )

    log(
        f"Source: {COMPLETE}"
    )

    log(
        f"Output: {ONGOING}"
    )


    # Создаём output-каталог при необходимости.
    ONGOING.mkdir(
        parents=True,
        exist_ok=True,
    )


    # ========================================================
    # БЕСКОНЕЧНЫЙ ЦИКЛ
    # ========================================================
    #
    # systemd следит за процессом.
    #
    # Если процесс неожиданно завершится,
    # systemd перезапустит его.
    # ========================================================

    while True:

        try:

            # Один проход поиска и обработки.
            scan()


        except Exception as exc:

            # Ошибка самого сканирования не должна
            # полностью остановить worker.
            log(
                f"Scan error: {exc}"
            )


        # Ждём до следующего сканирования.
        time.sleep(
            SCAN_INTERVAL
        )


# ============================================================
# ТОЧКА ВХОДА
# ============================================================

if __name__ == "__main__":

    main()
