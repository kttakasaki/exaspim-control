import numpy
import time
import logging
from ruamel.yaml import YAML
from pathlib import Path
from threading import Event, Thread, Lock
from multiprocessing.shared_memory import SharedMemory
from voxel.instruments.instrument import Instrument
from voxel.writers.data_structures.shared_double_buffer import SharedDoubleBuffer
from voxel.acquisition.acquisition import Acquisition
import inflection
from nidaqmx.constants import AcquisitionType as AcqType
import math

class ExASPIMAcquisition(Acquisition):

    def __init__(self, instrument: Instrument, config_filename: str, log_level='INFO'):
        self.log = logging.getLogger(f"{__name__}.{self.__class__.__name__}")
        self.log.setLevel(log_level)

        # current working directory
        this_dir = Path(__file__).parent.resolve()
        self.config_path = this_dir / Path(config_filename)
        self.config = YAML(typ='safe', pure=True).load(Path(self.config_path))
        self.acquisition = self.config['acquisition']
        self.instrument = instrument
        for operation_type, operation_dict in self.config['acquisition']['operations'].items():
            setattr(self, operation_type, dict())
            self._construct_operations(operation_type, operation_dict)
        self._verify_directories()
        self._verify_acquisition()

        self.acquisition_threads = dict()
        self.transfer_threads = dict()
        self.stop_engine = Event()  # Event to flag a stop in engine

    def _verify_acquisition(self):
        """Check that chunk sizes are the same for all writers"""
        super()._verify_acquisition()

        chunk_size = None
        for device in self.writers.values():
            for writer in device.values():
                if chunk_size is None:
                    chunk_size = writer.chunk_count_px
                else:
                    if writer.chunk_count_px != chunk_size:
                        raise ValueError (f'Chunk sizes of writers must all be {chunk_size}')
        self.chunk_count_px = chunk_size  # define chunk size to be used later in acquisiiton

    def run(self):

        filenames = dict()

        for tile in self.config['acquisition']['tiles']:

            tile_num = tile['tile_number']
            tile_channel = tile['channel']
            filename_prefix = tile['prefix']

            # build filenames dict for all devices
            for device_name, device_specs in self.instrument.config['instrument']['devices'].items():
                device_type = device_specs['type']
                filenames[device_name] = f'{filename_prefix}_{tile_num}_' \
                                         f'ch_{tile_channel}_{device_type}_{device_name}'
            # sanity check length of scan
            for writer_dictionary in self.writers.values():
                for writer in writer_dictionary.values():
                    chunk_count_px = writer.chunk_count_px
                    tile_count_px = tile['steps']
                    if tile_count_px < chunk_count_px:
                        raise ValueError(f'tile frame count {tile_count_px} \
                            is less than chunk size = {writer.chunk_count_px} px')

            # move all tiling stages to correct positions
            for tiling_stage_id, tiling_stage in self.instrument.tiling_stages.items():
                # grab stage axis letter
                instrument_axis = tiling_stage.instrument_axis
                tile_position = tile['position_mm'][instrument_axis]
                self.log.info(f'moving stage {tiling_stage_id} to {instrument_axis} = {tile_position} mm')
                tiling_stage.move_absolute_mm(tile_position, wait=False)

            # wait on all stages... simultaneously
            for tiling_stage_id, tiling_stage in self.instrument.tiling_stages.items():
                while tiling_stage.is_axis_moving():
                    instrument_axis = tiling_stage.instrument_axis
                    tile_position = tile['position_mm'][instrument_axis]
                    self.log.info(
                        f'waiting for stage {tiling_stage_id}: {instrument_axis} = {tiling_stage.position_mm} -> {tile_position} mm')
                    time.sleep(1.0)

            # prepare the scanning stage for step and shoot behavior
            for scanning_stage_id, scanning_stage in self.instrument.scanning_stages.items():
                self.log.info(f'setting up scanning stage: {scanning_stage_id}')
                # grab stage axis letter
                instrument_axis = scanning_stage.instrument_axis
                tile_position = tile['position_mm'][instrument_axis]
                self.log.info(f'moving stage {scanning_stage_id} to {instrument_axis} = {tile_position} mm')
                scanning_stage.move_absolute_mm(tile_position, wait=False)

            # setup channel i.e. laser and filter wheels
            self.log.info(f'setting up channel: {tile_channel}')
            channel = self.instrument.channels[tile_channel]
            for device_type, devices in channel.items():
                for device_name in devices:
                    device = getattr(self.instrument, device_type)[device_name]
                    if device_type in ['lasers', 'filters']:
                        device.enable()
                    for setting, value in tile.get(device_name, {}).items():
                        setattr(device, setting, value)
                        self.log.info(f'setting {setting} for {device_type} {device_name} to {value}')

            for daq_name, daq in self.instrument.daqs.items():
                if daq.tasks.get('ao_task', None) is not None:
                    daq.add_task('ao')
                    daq.generate_waveforms('ao', tile_channel)
                    daq.write_ao_waveforms()
                if daq.tasks.get('do_task', None) is not None:
                    daq.add_task('do')
                    daq.generate_waveforms('do', tile_channel)
                    daq.write_do_waveforms()
                if daq.tasks.get('co_task', None) is not None:
                    pulse_count = self.chunk_count_px
                    daq.add_task('co', pulse_count)

            # run any pre-routines for all devices
            for device_name, routine_dictionary in getattr(self, 'routines', {}).items():
                device_type = self.instrument.config['instrument']['devices'][device_name]['type']
                self.log.info(f'running routines for {device_type} {device_name}')
                for routine_name, routine in routine_dictionary.items():
                    # TODO: how to figure out what to pass in routines for different devices.
                    # config seems like a good place but what about arguments generated in the acquisition?
                    # make it a rule that routines must have filename property? And need to pass in device to start?
                    device_object = getattr(self.instrument, inflection.pluralize(device_type))[device_name]
                    routine.filename = filenames[device_name] + '_' + routine_name
                    routine.start(device=device_object)

            # setup camera, data writing engines, and processes
            for camera_id, camera in self.instrument.cameras.items():
                self.log.info(f'arming camera and writer for {camera_id}')
                # pass in camera specific camera, writer, and processes
                # a filename must exist for each camera
                filename = filenames[camera_id]
                # a writer must exist for each camera
                writer = self.writers[camera_id]
                # check if any processes exist, they may not exist
                processes = {} if not hasattr(self, 'processes') else self.processes[camera_id]
                thread = Thread(target=self.engine,
                                          args=(tile,
                                                filename,
                                                camera,
                                                writer,
                                                processes,
                                                ))
                self.acquisition_threads[camera_id] = thread

            # start and arm the slaved cameras/writers
            for camera_id in self.acquisition_threads:
                self.log.info(f'starting camera and writer for {camera_id}')
                self.acquisition_threads[camera_id].start()

            # wait for the cameras/writers to finish
            for camera_id in self.acquisition_threads:
                self.log.info(f'waiting for camera {camera_id} to finish')
                self.acquisition_threads[camera_id].join()

            # stop the daq
            for daq_id, daq in self.instrument.daqs.items():
                self.log.info(f'stopping daq {daq_id}')
                daq.stop()

            # handle starting and waiting for file transfers
            for device_name, transfer_dict in self.transfer_threads.items():
                for transfer_id, transfer_thread in transfer_dict.items():
                    if transfer_thread.is_alive():
                        self.log.info(f"waiting on file transfer for {device_name} {transfer_id}")
                        transfer_thread.wait_until_finished()
            self.transfer_threads = {}  # clear transfer threads

            # create and start transfer threads from previous tile
            for device_name, transfer_dict in getattr(self, 'transfers', {}).items():
                self.transfer_threads[device_name] = {}
                for transfer_name, transfer in transfer_dict.items():
                    self.transfer_threads[device_name][transfer_name] = transfer
                    self.transfer_threads[device_name][transfer_name].filename = filenames[device_name]
                    self.log.info(f"starting file transfer for {device_name}")
                    self.transfer_threads[device_name][transfer_name].start()

        # wait for last tiles file transfer
        # TODO: We seem to do this logic a lot of looping through device then op.
        # Should this be a function?
        for device_name, transfer_dict in getattr(self, 'transfers', {}).items():
            for transfer_id, transfer_thread in transfer_dict.items():
                if transfer_thread.is_alive():
                    self.log.info(f"waiting on file transfer for {device_name} {transfer_id}")
                    transfer_thread.wait_until_finished()

    def engine(self, tile, filename, camera, writers, processes):

        chunk_locks = {}
        img_buffers = {}

        # setup writers
        for writer_name, writer in writers.items():
            writer.row_count_px = camera.height_px
            writer.column_count_px = camera.width_px
            writer.frame_count_px = tile['steps']
            writer.x_pos_mm = tile['position_mm']['x']
            writer.y_pos_mm = tile['position_mm']['y']
            writer.z_pos_mm = tile['position_mm']['z']
            writer.x_voxel_size_um = 0.748
            writer.y_voxel_size_um = 0.748
            writer.z_voxel_size_um = tile['step_size']
            writer.filename = filename
            writer.channel = tile['channel']

            chunk_locks[writer_name] = Lock()
            img_buffers[writer_name] = SharedDoubleBuffer(
                (writer.chunk_count_px, camera.height_px, camera.width_px),
                dtype=writer.data_type)

        # setup processes
        process_buffers = {}
        for process_name, process in processes.items():
            process.row_count_px = camera.height_px
            process.column_count_px = camera.width_px
            process.frame_count_px = tile['steps']
            process.filename = filename
            img_bytes = numpy.prod(camera.height_px * camera.width_px) * numpy.dtype(
                process.data_type).itemsize
            buffer = SharedMemory(create=True, size=int(img_bytes))
            process_buffers[process_name] = buffer
            process.buffer_image = numpy.ndarray((camera.height_px, camera.width_px),
                                                 dtype=process.data_type, buffer=buffer.buf)
            process.prepare(buffer.name)

        # set up writer and camera
        camera.prepare()
        for writer in writers.values():
            writer.prepare()
            writer.start()
        camera.start()
        for process in processes.values():
            process.start()

        frame_index = 0
        last_frame_index = tile['steps'] - 1

        chunk_count = math.ceil(tile['steps'] / self.chunk_count_px)
        remainder = tile['steps'] % self.chunk_count_px
        last_chunk_size = self.chunk_count_px if not remainder else remainder

        # Images arrive serialized in repeating channel order.
        for stack_index in range(tile['steps']):
            if self.stop_engine.is_set():
                break
            chunk_index = stack_index % self.chunk_count_px
            # Start a batch of pulses to generate more frames and movements.
            if chunk_index == 0:
                chunks_filled = math.floor(stack_index / self.chunk_count_px)
                remaining_chunks = chunk_count - chunks_filled
                num_pulses = last_chunk_size if remaining_chunks == 1 else self.chunk_count_px
                for daq_name, daq in self.instrument.daqs.items():
                    # TODO line below breaks simulated mode...
                    daq.co_task.timing.cfg_implicit_timing(sample_mode= AcqType.FINITE,
                                                            samps_per_chan= num_pulses)
                    #################### IMPORTANT ####################
                    # for the exaspim, the NIDAQ is the master, so we start this last
                    for daq_id, daq in self.instrument.daqs.items():
                        self.log.info(f'starting daq {daq_id}')
                        for task in [daq.ao_task, daq.do_task, daq.co_task]:
                            if task is not None:
                                task.start()

            # Grab camera frame
            current_frame = camera.grab_frame()
            camera.signal_acquisition_state()
            # TODO: Update writer variables?
            # writer.signal_progress_percent
            for img_buffer in img_buffers.values():
                img_buffer.add_image(current_frame)

            # Dispatch either a full chunk of frames or the last chunk,
            # which may not be a multiple of the chunk size.
            if chunk_index + 1 == self.chunk_count_px or stack_index == last_frame_index:
                for daq_name, daq in self.instrument.daqs.items():
                    daq.stop()
                while not writer.done_reading.is_set() and not self.stop_engine.is_set():
                    time.sleep(0.001)
                for writer_name, writer in writers.items():
                    # Dispatch chunk to each StackWriter compression process.
                    # Toggle double buffer to continue writing images.
                    # To read the new data, the StackWriter needs the name of
                    # the current read memory location and a trigger to start.
                    # Lock out the buffer before toggling it such that we
                    # don't provide an image from a place that hasn't been
                    # written yet.
                    with chunk_locks[writer_name]:
                        img_buffers[writer_name].toggle_buffers()
                        if writer.path is not None:
                            writer.shm_name = \
                                img_buffers[writer_name].read_buf_mem_name
                            writer.done_reading.clear()

            # check on processes
            for process in processes.values():
                while process.new_image.is_set():
                    time.sleep(0.1)
                process.buffer_image[:, :] = current_frame
                process.new_image.set()

            frame_index += 1

        for writer in writers.values():
            writer.wait_to_finish()

        for process in processes.values():
            process.wait_to_finish()
            # process.close()

        camera.stop()

        # clean up the image buffer
        self.log.debug(f"deallocating shared double buffer.")
        for img_buffer in img_buffers.values():
            img_buffer.close_and_unlink()
            del img_buffer
        for buffer in process_buffers.values():
            buffer.close()
            buffer.unlink()
            del buffer

    def stop_acquisition(self):
        """Overwriting to better stop acquisition"""

        self.stop_engine.set()
        for thread in self.acquisition_threads.values():
            thread.join()

        # TODO: Stop any devices here? or stop transfer_threads?

        raise RuntimeError
