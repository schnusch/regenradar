# regenradar

Generate a precipitation radar film for Germany from OpenStreetMap maps and
[DWD](https://www.dwd.de/) data.

![Preview](https://github.com/schnusch/regenradar/raw/refs/heads/gh-pages/out/dresden.mp4)

It does this the worst way imaginable: A webpage is created, this page uses
[Leaflet](https://leafletjs.com/) to display an OpenStreetMap map. The
[DWD's precipitation radar](https://www.dwd.de/DE/leistungen/radarprodukte/radarlayer.html?nn=16102&lsbId=401764) 
is added as another layout to the map. Then [Selenium](https://selenium-python.readthedocs.io/api.html)
is used to create a screenshot of the map. Finally [FFmpeg](https://ffmpeg.org/)
concatenates the screenshots to a MP4 video.
