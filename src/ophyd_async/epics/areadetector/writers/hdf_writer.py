import asyncio
from typing import (
    AsyncIterator,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Sequence,
    Union,
)

from bluesky.protocols import Descriptor
from event_model import StreamDatum, StreamResource

from ophyd_async.core import (
    DEFAULT_TIMEOUT,
    AsyncStatus,
    DetectorWriter,
    DirectoryProvider,
    NameProvider,
    set_and_wait_for_value,
    wait_for_value,
)

from ._hdfdataset import _HDFDataset
from ._hdffile import _HDFFile
from .nd_file_hdf import FileWriteMode, NDFileHDF


class HDFWriter(DetectorWriter):
    def __init__(
        self,
        hdf: NDFileHDF,
        directory_provider: DirectoryProvider,
        name_provider: NameProvider,
        shape_provider: Callable[[None], Awaitable[Sequence[int]]],
        **scalar_datasets_paths: str,
    ) -> None:
        self.hdf = hdf
        self._directory_provider = directory_provider
        self._name_provider = name_provider
        self._shape_provider = shape_provider
        self._scalar_datasets_paths = scalar_datasets_paths
        self._capture_status: Optional[AsyncStatus] = None
        self._datasets: List[_HDFDataset] = []
        self._file: Optional[_HDFFile] = None
        self._multiplier = 1

    async def open(self, multiplier: int = 1) -> Dict[str, Descriptor]:
        self._file = None
        info = self._directory_provider()
        await asyncio.gather(
            self.hdf.lazy_open.set(True),
            self.hdf.swmr_mode.set(True),
            self.hdf.file_path.set(info.directory_path),
            self.hdf.file_name.set(f"{info.filename_prefix}{self.hdf.name}"),
            self.hdf.file_template.set("%s/%s.h5"),
            # Go forever
            self.hdf.num_capture.set(0),
            self.hdf.file_write_mode.set(FileWriteMode.stream),
        )
        # Wait for it to start, stashing the status that tells us when it finishes
        self._capture_status = await set_and_wait_for_value(self.hdf.capture, True)
        name = self._name_provider()
        detector_shape = tuple(await self._shape_provider())
        self._multiplier = multiplier
        if multiplier > 1:
            outer_shape = (multiplier,)
        else:
            outer_shape = ()
        # Add the main data
        self._datasets = [_HDFDataset(name, "/entry/data", detector_shape, multiplier)]
        # And all the scalar datasets
        for ds_name, ds_path in self._scalar_datasets_paths.items():
            self._datasets.append(
                _HDFDataset(f"{name}.{ds_name}", f"/entry/{ds_path}", (), multiplier)
            )
        describe = {
            ds.name: Descriptor(
                source=self.hdf.full_file_name.source,
                shape=outer_shape + ds.shape,
                dtype="array",
                external="STREAM:",
            )
            for ds in self._datasets
        }
        return describe

    async def wait_for_index(self, index: int):
        def matcher(value: int) -> bool:
            return value // self._multiplier >= index

        matcher.__name__ = f"index_at_least_{index}"
        await wait_for_value(self.hdf.num_captured, matcher, timeout=None)

    async def get_indices_written(self) -> int:
        num_captured = await self.hdf.num_captured.get_value()
        return num_captured // self._multiplier

    async def reset_index(self) -> None:
        """To be implemented."""
        ...

    async def collect_stream_docs(
        self, indices_written: int
    ) -> AsyncIterator[Union[StreamResource, StreamDatum]]:
        # TODO: fail if we get dropped frames
        await self.hdf.flush_now.execute()
        if indices_written and not self._file:
            self._file = _HDFFile(
                await self.hdf.full_file_name.get_value(), self._datasets
            )
            for doc in self._file.stream_resources():
                yield doc
        for doc in self._file.stream_data(indices_written):
            yield doc

    async def close(self):
        # Already done a caput callback in _capture_status, so can't do one here
        await self.hdf.capture.set(0, wait=False)
        await wait_for_value(self.hdf.capture, 0, DEFAULT_TIMEOUT)
        if self._capture_status:
            # We kicked off an open, so wait for it to return
            await self._capture_status