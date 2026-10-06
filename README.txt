anime-converter
===============

Automatic anime video transcoding worker for Ubuntu/Debian.

Architecture
============

Debian server:
    10.8.1.3

    transmission-daemon
    |
    +-- /home/chaser/data/jormungandr/transmission/incomplete
    +-- /home/chaser/data/jormungandr/transmission/complete
    +-- /home/chaser/data/jormungandr/transmission/tfiles
    +-- /home/chaser/data/jormungandr/ongoing


Ubuntu worker:
    10.8.1.4

    /home/chaser/data/jormungandr
        |
        +-- NFS mount from 10.8.1.3
        |
        +-- transmission/complete
        |
        +-- ongoing


The worker only scans:

    /home/chaser/data/jormungandr/transmission/complete

It never scans or modifies:

    /home/chaser/data/jormungandr/transmission/incomplete


What the converter does
=======================

The worker recursively scans the Transmission complete directory.

Videos may be located:

    directly in complete/

or in arbitrary nested directories.

Supported video extensions:

    .mkv
    .mp4
    .avi
    .m4v
    .mov
    .ts
    .webm
    .flv


Output
======

Output is written to:

    /home/chaser/data/jormungandr/ongoing

The directory structure below complete/ is preserved.

Example:

    complete/Anime/Season 1/Episode 01.mkv

becomes:

    ongoing/Anime/Season 1/Episode 01.mkv


Transcoding
===========

Video:

    codec: libx264
    resolution: 720p
    preset: veryfast

Audio:

    codec: libmp3lame
    bitrate: 192k

Container:

    MKV


Language priority
=================

Russian has highest priority.

Priority:

    1. Russian
    2. Japanese
    3. English

Recognized language identifiers include:

Russian:
    ru
    rus
    russian
    рус
    русский

Japanese:
    ja
    jp
    jpn
    japanese
    яп
    японский

English:
    en
    eng
    english


Audio and subtitles
===================

The converter examines internal audio/subtitle streams using ffprobe.

If matching internal tracks are found, they are included in the output.

External audio files can also be detected.

Recognized external audio extensions:

    .mka
    .aac
    .mp3
    .ac3
    .eac3
    .flac
    .wav
    .ogg
    .opus

Recognized external subtitle extensions:

    .ass
    .ssa
    .srt
    .vtt
    .sup
    .sub


Known audio directories:

    audio
    audios
    sound
    sounds
    voice
    voices
    dub
    dubs
    озвучка
    аудио


Known subtitle directories:

    sub
    subs
    subtitle
    subtitles
    субтитры


External track matching
========================

External tracks are matched to video files using:

    - filename similarity
    - episode numbers
    - language identifiers
    - known audio/subtitle directory names

The matching is intentionally conservative.

It should be tested on a representative sample of the collection before
production use with automatic source deletion enabled.


Source deletion
===============

The original source video is deleted only after:

    1. ffmpeg completes successfully
    2. the temporary output is successfully created
    3. ffprobe validates the resulting file
    4. the final output is moved into place


Temporary files
===============

During encoding the output has:

    .processing

suffix.

Example:

    Episode 01.mkv.processing

This prevents an incomplete encode from looking like a finished file.

Temporary files are removed after failed conversions when possible.


Duplicate protection
====================

The worker uses:

    /home/chaser/data/jormungandr/.anime-converter.lock

with flock().

This prevents multiple worker instances from processing the collection
simultaneously.


Systemd
=======

Service name:

    anime-converter.service


Install:

    sudo ./install.sh


Check status:

    sudo systemctl status anime-converter


Follow logs:

    sudo journalctl -u anime-converter -f


Restart:

    sudo systemctl restart anime-converter


Stop:

    sudo systemctl stop anime-converter


Disable:

    sudo systemctl disable anime-converter


Files installed by install.sh
==============================

Python worker:

    /usr/local/bin/anime-converter.py

systemd unit:

    /etc/systemd/system/anime-converter.service


Requirements
============

Ubuntu/Debian worker server with:

    Python 3
    ffmpeg
    ffprobe
    NFS-mounted media directory

The worker does not require Transmission.

Transmission itself runs on the Debian server using:

    transmission-daemon

The worker only processes files that have already appeared in:

    transmission/complete


Important
=========

Before enabling automatic processing over the entire collection, verify
external audio/subtitle matching on a small representative sample.

The current converter does not provide a dry-run mode yet.

Recommended first step is therefore to temporarily disable automatic
source deletion or test against a small sample before processing the
whole library.
