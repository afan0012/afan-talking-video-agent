# Third-party notices

This application may include the following separately licensed component.

## FFmpeg

The Windows package embeds the **GPL v3** build of FFmpeg (the
`release-essentials` build from <https://www.gyan.dev/ffmpeg/builds/>, which
includes the `libx264` encoder used for video export).  The applicable license
text ships with the package in the `_internal/licenses` directory
(`COPYING.GPLv3.txt`), and FFmpeg's source code is available from
<https://ffmpeg.org/download.html> and
<https://www.gyan.dev/ffmpeg/builds/>.

When running from source, the startup script can auto-download an equivalent
FFmpeg build (`ffmpeg-static` via the npmmirror registry), which is likewise
GPL-licensed; users may alternatively install any FFmpeg build that provides
the H.264 encoder.

This application invokes `ffmpeg.exe` as a separate process.  FFmpeg and this
application are independent programs; this notice does not change the license
of this application's own source code.
