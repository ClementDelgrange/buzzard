import functools
import collections

import multiprocessing as mp
import multiprocessing.pool
import numpy as np

from buzzard._actors.message import Msg
from buzzard._actors.pool_job import ProductionJobWaiting, PoolJobWorking

class ActorResampler(object):
    """Actor that takes care of resamplig sample tiles to produce tiles"""

    def __init__(self, raster):
        self._raster = raster
        self._alive = True
        io_pool = raster.io_pool
        self._waiting_room_address = '/Pool{}/WaitingRoom'.format(id(io_pool))
        self._working_room_address = '/Pool{}/WorkingRoom'.format(id(io_pool))
        self._waiting_jobs = set()
        self._working_jobs = set()
        if isinstance(io_pool, mp.ThreadPool):
            self._same_address_space = True
        elif isinstance(io_pool, mp.Pool):
            self._same_address_space = False
        else:
            assert False, 'Type should be checked in facade'

        self._prod_array_of_prod_tile = (
            collections.defaultdict(dict)
        ) # type: Mapping[CachedQueryInfos, Mapping[int, np.ndarray]]
        self._missing_resample_fps_per_prod_tile = (
            collections.defaultdict(dict)
        ) # type: Mapping[CachedQueryInfos, Mapping[int, Set[Footprint]]]


    @property
    def address(self):
        return '/Raster{}/Resampler'.format(self._raster.uid)

    @property
    def alive(self):
        return self._alive

    # ******************************************************************************************* **
    def receive_resample_and_accumulate(self, qi, prod_idx, sample_fp, resample_fp, sample_array):
        msgs = []
        wait = Wait(self, qi, prod_idx, sample_fp, resample_fp, sample_array)
        self._waiting_jobs.add(wait)
        msgs += [
            Msg(self._waiting_room_address, 'schedule_job', wait)
        ]
        return msgs

    def receive_token_to_working_room(self, job, token):
        msgs = []
        self._waiting_jobs.remove(job)

        prod_idx = job.prod_idx
        qi = job.qi
        prod_fp = qi.prod[prod_idx].fp

        if prod_idx not in self._prod_array_of_prod_tile[qi]:
            # Allocate prod array
            self._prod_array_of_prod_tile[qi][prod_idx] = np.empty(
                np.r_[prod_fp.shape, len(qi.band_ids)],
                qi.dtype,
            )
            self._missing_resample_fps_per_prod_tile[qi][prod_idx] = set(qi.prod[prod_idx].resample_fps)
        dst_array = self._prod_array_of_prod_tile[qi][prod_idx]

        work = Work(self, job.qi, job.prod_idx, job.sample_fp, job.resample_fp, job.sample_array, dst_array)
        self._working_jobs.add(work)
        return [
            Msg(self._working_room_address, 'launch_job_with_token', work, token)
        ]

        return msgs

    def receive_job_done(self, job, result):
        msgs = []
        if self._same_address_space:
            assert result is None
        else:
            job.dst_array_slice[:] = result

        self._working_jobs.remove(job)
        dst_array = self._prod_array_of_prod_tile[job.qi][job.prod_idx]
        self._missing_resample_fps_per_prod_tile[job.qi][job.prod_idx].remove(job.resample_fp)

        if len(self._missing_resample_fps_per_prod_tile[job.qi][job.prod_idx]) == 0:
            msgs += [
                Msg('Producer', 'made_this_array',
                    job.qi, job.prod_idx, dst_array,
                )
            ]

        if len(self._missing_resample_fps_per_prod_tile[job.qi][job.prod_idx]) == 0:
            # Done resampling for that `(qi, prod_idx)`
            del self._missing_resample_fps_per_prod_tile[job.qi][job.prod_idx]
            del self._prod_array_of_prod_tile[job.qi][job.prod_idx]

        if len(self._missing_resample_fps_per_prod_tile[job.qi]) == 0:
            # Not resampling for that `qi`
            del self._missing_resample_fps_per_prod_tile[job.qi]
            del self._prod_array_of_prod_tile[job.qi]

        return msgs

    def receive_cancel_this_query(self, qi):
        """Receive message: One query was dropped

        Parameters
        ----------
        qi: _actors.cached.query_infos.QueryInfos
        """
        msgs = []
        # Cancel waiting jobs
        jobs_to_kill = [
            job
            for job in self._waiting_jobs
            if job.qi == qi
        ]
        for job in jobs_to_kill:
            msgs += [Msg(self._waiting_room_address, 'unschedule_job', job)]
            self._waiting_jobs.remove(job)

        # Cancel working jobs
        jobs_to_kill = [
            job
            for job in self._working_jobs
            if job.qi == qi
        ]
        for job in self._working_jobs:
            msgs += [Msg(self._working_room_address, 'cancel_job', job)]
            self._working_jobs.remove(job)

        return []

    def receive_die(self):
        """Receive message: The raster was killed"""
        assert self._alive
        self._alive = False

        msgs = []
        for job in self._waiting_jobs:
            msgs += [Msg(self._waiting_room_address, 'unschedule_job', job)]
        for job in self._working_jobs:
            msgs += [Msg(self._working_room_address, 'cancel_job', job)]
        self._waiting_jobs.clear()
        self._working_jobs.clear()

        return []

    # ******************************************************************************************* **

class Wait(ProductionJobWaiting):

    def __init__(self, actor, qi, prod_idx, sample_fp, resample_fp, sample_array):
        self.qi = qi
        self.prod_idx = prod_idx
        self.sample_fp = sample_fp
        self.resample_fp = resample_fp
        self.sample_array = sample_array
        # TODO: set action priority other than 1
        super().__init__(actor.address, qi, prod_idx, 1, self.resample_fp)

class Work(PoolJobWorking):
    def __init__(self, actor, qi, prod_idx, sample_fp, resample_fp, sample_array, dst_array):
        self.qi = qi
        self.prod_idx = prod_idx
        produce_fp = qi.prod[prod_idx].fp

        dst_array_slice = dst_array[resample_fp.slice_in(produce_fp)]

        if actor._same_address_space:
            func = functools.partial(
                _resample_sample_array,
                sample_fp, resample_fp, sample_array, qi.dst_nodata, qi.interpolation, dst_array, 
            )
        else:
            self._dst_array_slice = dst_array_slice
            func = functools.partial(
                _resample_sample_array,
                sample_fp, resample_fp, sample_array, qi.dst_nodata, qi.interpolation, None,
            )

        super().__init__(actor.address, func)

def _resample_sample_array(sample_fp, resample_fp, sample_array, dst_nodata, interpolation, dst_opt):
    """
    Parameters
    ----------
    sample_fp: Footprint
        source footprint (before resampling)
    resample_fp: Footprint
        destination footprint
    sample_array: np.ndarray
        source array (sould match sample_fp)
    dst_nodata: float
    interpolation: str
    dst_opt: None or np.ndarray
        optional destination for resample
    """
    assert (True or False) == 'That is the TODO question'

    if dst_opt is not None:
        return None
    else:
        return arr