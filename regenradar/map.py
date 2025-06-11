"""
regenradar
Copyright (C) 2024-2025  schnusch

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as
published by the Free Software Foundation, either version 3 of the
License, or (at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""

import argparse
import asyncio
import logging
import os
import shlex
import subprocess
import sys
import textwrap
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import (
    Any,
    AsyncIterator,
    Coroutine,
    Iterator,
    List,
    Optional,
    Sequence,
    Tuple,
    TypeVar,
    Union,
)

import aiohttp
import importlib_resources
from selenium.webdriver import Firefox
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.remote.webelement import WebElement

logger = logging.getLogger(__name__)
html_resources = importlib_resources.files(f"{__package__}.html")


@contextmanager
def firefox_driver(
    headless: bool = True,
    minimize: bool = False,
) -> Iterator[Firefox]:
    options = FirefoxOptions()
    if headless:
        options.add_argument("--headless")
    driver = Firefox(options=options)
    try:
        if minimize and not headless:
            driver.minimize_window()
        yield driver
    finally:
        driver.close()


@asynccontextmanager
async def ffmpeg_png_to_mp4_pipe(
    output: Union[str, bytes, os.PathLike[str], os.PathLike[bytes]],
    *,
    framerate: Optional[int] = None,
    crf: Optional[int] = None,
) -> AsyncIterator[asyncio.StreamWriter]:
    input_format = (
        "-f",
        "image2pipe",
        "-c:v",
        "png",
        "-framerate",
        str(2 if framerate is None else framerate),
    )
    # compatible with Telegram video messages
    output_format = (
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-profile:v",
        "baseline",
        "-crf",
        str(18 if crf is None else crf),
        "-preset",
        "veryslow",
        "-f",
        "mp4",
        "-movflags",
        "+faststart",
    )
    cmd = [
        "ffmpeg",
        "-hide_banner",
        *input_format,
        "-i",
        "pipe:0",
        *output_format,
        "-y",
        output,
    ]
    p = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=subprocess.PIPE,
        stdout=sys.stderr,
    )
    try:
        assert p.stdin is not None
        yield p.stdin
        p.stdin.close()
        await p.stdin.wait_closed()
        rc = await p.wait()
        if rc != 0:
            raise subprocess.CalledProcessError(rc, cmd)
    except BaseException:
        if p.returncode is None:
            p.kill()
            await p.wait()
        raise


def isoformatz(t: datetime) -> str:
    s = t.isoformat()
    parts = s.rsplit("+", 1)
    if len(parts) > 1 and parts[1] == "00:00":
        return parts[0] + "Z"
    else:
        return s


async def dwd_tiles_exist(t: datetime) -> bool:
    # we test if the layer preview exists
    async with aiohttp.ClientSession() as session:
        async with session.head(
            "https://maps.dwd.de/geoserver/dwd/wms",
            params={
                "service": "WMS",
                "version": "1.1.0",
                "request": "GetMap",
                "layers": "dwd:Niederschlagsradar",
                "bbox": "-543.462,-4808.645,556.538,-3608.645",
                "width": "703",
                "height": "768",
                "srs": "EPSG:1000001",
                "format": "image/png",
                "time": isoformatz(t),
            },
        ) as resp:
            return resp.headers["Content-Type"] == "image/png"


T = TypeVar("T")


class WaitConditionFailed(Exception):
    pass


async def wait_conditional(
    main: Coroutine[Any, Any, T],
    condition: Coroutine[Any, Any, bool],
) -> T:
    """Run ``main`` and ``condition`` in parallel. ``condition`` is run to
    completion. If ``condition`` returns ``False`` ``m̀ain`` is cancelled and
    ``WaitConditionFailed`` is raised. If ``condition`` return ``True`` the
    result of ``main`` is returned.
    """
    main_task = asyncio.create_task(main)  # type: asyncio.Task[T]
    cond_task = asyncio.create_task(condition)  # type: asyncio.Task[bool]
    pending = {main_task, cond_task}
    try:
        done, pending = await asyncio.wait(
            pending,
            return_when=asyncio.FIRST_COMPLETED,
        )
        assert len(done) == 1
        task = done.pop()
        if task is cond_task:
            if not cond_task.result():
                raise WaitConditionFailed
            await main_task
        else:
            assert task is main_task
            if not await cond_task:
                raise WaitConditionFailed
        pending = set()
        return main_task.result()
    finally:
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending)


LatLng = Tuple[float, float]
LatLngBounds = Tuple[LatLng, LatLng]


async def create_radar_film(
    name: os.PathLike,
    *,
    bounds: Optional[LatLngBounds] = None,
    center: Optional[LatLng] = None,
    radius: Optional[Union[int, float]] = None,
    start: Union[timedelta, datetime] = -timedelta(hours=1),
    duration: timedelta = timedelta(hours=3),
    width: int = 1080,
    height: int = 1080,
    opacity: float = 0.8,
    framerate: Optional[int] = None,
    crf: Optional[int] = None,
    headless: bool = True,
) -> int:
    if bounds is None and (center is None or radius is None):
        raise TypeError("either bounds or center and radius must be set")
    if isinstance(start, timedelta):
        now = datetime.now(tz=timezone.utc)
        start = now + start
    else:
        start = start.astimezone(tz=timezone.utc)
    start -= timedelta(
        minutes=start.minute % 5,
        seconds=start.second,
        microseconds=start.microsecond,
    )
    end = start + duration
    step = timedelta(minutes=5)

    loop = asyncio.get_running_loop()

    frames = 0
    with importlib_resources.as_file(
        html_resources
    ) as resources_dir, TemporaryDirectory(
        dir=Path(name).parent, prefix=".tmp."
    ) as _temp:
        temp = Path(_temp) / "temp.mp4"
        async with ffmpeg_png_to_mp4_pipe(temp, framerate=framerate, crf=crf) as pipe:
            with firefox_driver(headless=headless) as driver:
                driver.get((resources_dir / "map.html").absolute().as_uri())

                await asyncio.sleep(1)

                # callback is appended to execute_async_script's arguments
                js = textwrap.dedent(
                    """\
                    ((t, bounds, center, radius, options, callback) => {
                        document.body.innerHTML = "";
                        load_map(new Date(t), bounds, center, radius, options)
                            .then(xs => callback([xs[0], null]))
                            .catch(e => callback([null, {message: e.message, stack: e.stack}]));
                    })(...arguments);
                    """.rstrip()
                )

                def execute_js(t: datetime) -> WebElement:
                    map_elem, error = driver.execute_async_script(
                        js,
                        int(t.timestamp()) * 1000,
                        bounds,
                        center,
                        radius,
                        {"width": width, "height": height, "opacity": opacity},
                    )
                    if error is not None:
                        raise ValueError(
                            "a javascript error occured: %s\nerror stack:\n%s"
                            % (
                                error["message"],
                                error["stack"].rstrip("\n"),
                            )
                        )
                    return map_elem

                async def render_map(t: datetime) -> WebElement:
                    return await loop.run_in_executor(None, execute_js, t)

                t = start
                while t <= end:
                    try:
                        map_elem = await wait_conditional(
                            render_map(t),
                            condition=dwd_tiles_exist(t),
                        )
                    except WaitConditionFailed:
                        logger.warning(
                            "radar image for %s does not exist", t.isoformat()
                        )
                    else:
                        pipe.write(map_elem.screenshot_as_png)
                        await pipe.drain()
                        frames += 1
                    t += step

                logger.info("done")
            if frames == 0:
                raise ValueError(f"no video frames produced from {start} to {end}")
        os.replace(temp, name)
    return frames


def strptime_multi(t: str, fmts: Sequence[str]) -> datetime:
    try:
        return datetime.strptime(t, fmts[0])
    except ValueError:
        if len(fmts) > 1:
            return strptime_multi(t, fmts[1:])
        else:
            raise


def parse_duration(x: str) -> timedelta:
    t = strptime_multi(x, ["%H:%M:%S", "%H:%M", "%H"]).time()
    return timedelta(
        hours=t.hour,
        minutes=t.minute,
        seconds=t.second,
    )


def parse_timestamp_duration(x: str) -> Union[timedelta, datetime]:
    if x.startswith("+"):
        return parse_duration(x[1:])
    elif x.startswith("-"):
        return -parse_duration(x[1:])
    else:
        fmts = []  # type: List[str]
        for df in ("%Y-%m-%d",):
            for tf in ("%H:%M:%S", "%H:%M"):
                for z in ("", "Z"):
                    for t in ("", "T"):
                        fmts.append(f"{df}{t}{tf}{z}")
        print("\n".join(fmts))
        return strptime_multi(x, fmts)


def parse_lat_lng(x: str) -> LatLng:
    xs = x.split(",", 1)
    if len(xs) != 2:
        raise ValueError
    return (float(xs[0]), float(xs[1]))


def parse_boundary(x: str) -> LatLngBounds:
    xs = x.split(",", 2)
    if len(xs) != 3:
        raise ValueError
    return (
        parse_lat_lng(",".join(xs[:2])),
        parse_lat_lng(xs[2]),
    )


def main(argv: Optional[Sequence[str]] = None):
    p = argparse.ArgumentParser(
        description="Convert radar images to mp4 video.",
        epilog="Example (Dresden):\n$ "
        + shlex.join(
            [
                Path(sys.argv[0]).name,
                "-o",
                "dresden.mp4",
                "--center=51.0515487,13.7470293",
                f"--radius={100_000}",
                "--start=-1:30",
                "--duration=3:15",
            ],
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
    )
    p.add_argument("-o", "--output", required=True, type=Path)
    p.add_argument(
        "-s",
        "--start",
        type=parse_timestamp_duration,
        default=-timedelta(hours=1),
        help="starting time of the radar film in the following formats: %%Y-%%m-%%d(T| )%%H:%%M[:%%S][Z] or (+|-)%%H[:%%M[:%%S]]",
    )
    p.add_argument(
        "-t",
        "--duration",
        type=parse_duration,
        default=timedelta(hours=3),
        help="duration of the radar film in the following format: %%H[:%%M[:%%S]]",
    )
    p.add_argument("-w", "--width", type=int, default=1080)
    p.add_argument("-h", "--height", type=int, default=1080)
    p.add_argument("--crf", type=int, default=18)
    p.add_argument("-O", "--opacity", type=float, default=0.8)
    g = p.add_mutually_exclusive_group()
    g.add_argument(
        "-c", "--center", type=parse_lat_lng, help="fit to center and radius"
    )
    g.add_argument(
        "-b",
        "--bounds",
        type=parse_boundary,
        default=(
            (47.27011, 5.866316),
            (55.0585, 15.041925),
        ),
        help="fit map to bounds",
    )
    p.add_argument("-r", "--radius", type=int, default=100_000)
    p.add_argument("--help", action="help")
    p.add_argument(
        "-v",
        "--verbose",
        action="store_const",
        default=logging.INFO,
        const=logging.DEBUG,
        help="enable debug logging",
    )
    p.add_argument("-T", "--timeout", type=float, default=120)
    args = p.parse_args(argv)

    logging.basicConfig(level=args.verbose, stream=sys.stderr)

    asyncio.run(
        asyncio.wait_for(
            create_radar_film(
                args.output,
                bounds=args.bounds if args.center is None else None,
                center=args.center,
                radius=args.radius,
                start=args.start,
                duration=args.duration,
                width=args.width,
                height=args.height,
                opacity=args.opacity,
                framerate=4,
                headless=True,
                crf=args.crf,
            ),
            args.timeout,
        )
    )


if __name__ == "__main__":
    main()
