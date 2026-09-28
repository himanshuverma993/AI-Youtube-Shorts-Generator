"""Local backends — everything here runs on your own machine (no paid APIs).

The heavy lifting (downloading, cutting, vertical reframing) is done by
yt-dlp + ffmpeg + OpenCV; all AI calls live in the top-level modules and go
through the free Groq tier.
"""
