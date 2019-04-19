import sys

import numpy as np

from buzzard._a_source import ASource, ABackSource
from buzzard._a_source_raster_remap import ABackSourceRasterRemapMixin
from buzzard._footprint import Footprint
from buzzard import _tools

class ASourceRaster(ASource):
    """Base abstract class defining the common behavior of all rasters.

    Features Defined
    ----------------
    - Has a `stored` Footprint that defines the location of the raster
    - Has a Footprint that is influenced by the Dataset's opening mode
    - Has a lenght that defines how many bands are available
    - Has a `band_schema` that defines per band attributes (like nodata)
    - Has a `dtype` (like np.float32)
    - Has a `get_data` method that allows to read pixels in their current state to numpy arrays
    """

    @property
    def fp_stored(self):
        return self._back.fp_stored

    @property
    def fp(self):
        return self._back.fp

    @property
    def band_schema(self):
        return dict(self._back.band_schema)

    @property
    def dtype(self):
        return self._back.dtype

    @property
    def nodata(self):
        """Accessor for first band's nodata value"""
        return self._back.nodata

    def get_nodata(self, band=1):
        """Accessor for nodata value"""
        return self._back.get_nodata(band)

    def __len__(self):
        """Return the number of bands"""
        return len(self._back)

    @property
    def shared_band_id(self):
        return self._back.shared_band_id

    def get_data(self, fp=None, channels=-1, dst_nodata=None, interpolation='cv_area', **kwargs):
        """Read a rectangle of data on several channels from the source raster.

        If `fp` is not fully within the source raster, the external pixels are set to nodata. If
        nodata is missing, 0 is used.
        If `fp` is not on the same grid as the source raster, remapping is performed using
        `interpolation` algorithm. (It fails if the `allow_interpolation` parameter is set to
        False in `Dataset` (default)). When remapping, the nodata values are not interpolated,
        they are correctly spread to the output.

        If `dst_nodata` is provided, nodata pixels are set to `dst_nodata`.

        The alpha bands are currently resampled like any other band, this behavior may change in
        the future. To normalize an `rgba` array after a resampling operation, use this
        piece of code:
        >>> arr = np.where(arr[..., -1] == 255, arr, 0)

        /!\ Bands in GDAL are indexed from 1. Channels in buzzard are indexed from 0.

        Parameters
        ----------
        fp: Footprint of shape (Y, X) or None
            If None: return the full source raster
            If Footprint: return this window from the raster
        channels: int or sequence of int (see `Channels Parameter` below)
            The channels to be read
        dst_nodata: nbr or None
            nodata value in output array
            If None and raster.nodata is not None: raster.nodata is used
            If None and raster.nodata is None: 0 is used
        interpolation: one of {'cv_area', 'cv_nearest', 'cv_linear', 'cv_cubic', 'cv_lanczos4'} or None
            OpenCV method used if intepolation is necessary

        Returns
        -------
        array: numpy.ndarray of shape (Y, X) or (Y, X, C)
            If the `channels` parameter is `-1`, the returned array is of shape (Y, X) when `B=1`,
               (Y, X, B) otherwise.
            If the `channels` parameter is an integer `>=0`, the returned array is of shape (Y, X).
            If the `channels` parameter is a sequence, the returned array is always of shape (Y, X, B),
               no matter the size of `B`. Use `channels=[-1]` to get a monad containing all channels.
            (see `Channels Parameter` below)

        Channels Parameter
        ------------------
        | type            | value                         | meaning        | output shape        |
        |-----------------|-------------------------------|----------------|---------------------|
        | int             | -1                            | All channels   | (Y, X) or (Y, X, C) |
        | int             | 0, 1, 2, ...                  | Channel `i`    | (Y, X)              |
        | sequence of int | [-1]                          | All channels   | (Y, X, C)           |
        | sequence of int | [0], [1], [2], ...            | Channel `i`    | (Y, X, 1)           |
        | sequence of int | [0, 1, 2], [0, 2, -1, 0], ... | Those channels | (Y, X, C)           |

        """
        dst_nodata, kwargs = _tools.deprecation_pool.handle_param_renaming_with_kwargs(
            new_name='dst_nodata', old_names={'nodata': '0.5.0'}, context='ASourceRaster.get_data',
            new_name_value=dst_nodata,
            new_name_is_provided=dst_nodata != None,
            user_kwargs=kwargs,
        )

        def _band_to_channels(val):
            val = np.asarray(val)
            val = np.asarray([
                val[idx] - 1 if val[idx] >= 1 else val[idx]
                for idx in np.ndindex(val.shape)
            ]).reshape(val.shape)
            return val
        channels, kwargs = _tools.deprecation_pool.handle_param_renaming_with_kwargs(
            new_name='channels', old_names={'band': '0.5.1'}, context='ASourceRaster.get_data',
            new_name_value=channels,
            new_name_is_provided=channels != -1,
            user_kwargs=kwargs,
            transform_old=_band_to_channels,
        )
        if kwargs: # pragma: no cover
            raise TypeError("get_data() got an unexpected keyword argument '{}'".format(
                list(kwargs.keys())[0]
            ))

        # Normalize and check fp parameter
        if fp is None:
            fp = self.fp
        elif not isinstance(fp, Footprint): # pragma: no cover
            raise ValueError('`fp` parameter should be a Footprint (not {})'.format(fp))

        # Normalize and check channels parameter
        channel_ids, is_flat = _tools.normalize_channels_parameter(
            channels, len(self)
        )
        if is_flat:
            outshape = tuple(fp.shape)
        else:
            outshape = tuple(fp.shape) + (len(channel_ids),)
        del channels

        # Normalize and check dst_nodata parameter
        if dst_nodata is not None:
            dst_nodata = self.dtype.type(dst_nodata)
        elif self.nodata is not None:
            dst_nodata = self.nodata
        else:
            dst_nodata = self.dtype.type(0)

        # Check interpolation parameter
        if not (interpolation is None or
                interpolation in self._back.REMAP_INTERPOLATIONS): # pragma: no cover
            raise ValueError('`interpolation` should be None or one of {}'.format(
                set(self._back.REMAP_INTERPOLATIONS.keys())
            ))

        channel_ids = [i + 1 for i in channel_ids] # DEBUG!! # DEBUG!! # DEBUG!! # DEBUG!!

        return self._back.get_data(
            fp=fp,
            band_ids=channel_ids,
            dst_nodata=dst_nodata,
            interpolation=interpolation,
        ).reshape(outshape)

    # Deprecation
    fp_origin = _tools.deprecation_pool.wrap_property(
        'fp_stored',
        '0.4.4'
    )

class ABackSourceRaster(ABackSource, ABackSourceRasterRemapMixin):
    """Implementation of ASourceRaster's specifications"""

    def __init__(self, band_schema, dtype, fp_stored, **kwargs):
        super(ABackSourceRaster, self).__init__(rect=fp_stored, **kwargs)

        if self.to_work is not None:
            fp = fp_stored.move(*self.to_work([
                fp_stored.tl, fp_stored.tr, fp_stored.br
            ]))
        else:
            fp = fp_stored

        self.shared_band_id = None
        for i, type in enumerate(band_schema['mask'], 1):
            if type == 'per_dataset':
                self.shared_band_id = i
                break

        self.band_schema = band_schema
        self.dtype = dtype
        self.fp_stored = fp_stored

        self.fp = fp

    @property
    def nodata(self):
        return self.get_nodata(1)

    def get_nodata(self, band=1):
        return self.band_schema['nodata'][band - 1]

    def __len__(self):
        return len(self.band_schema['nodata'])

    def get_data(self, fp, band_ids, dst_nodata, interpolation): # pragma: no cover
        raise NotImplementedError('ABackSourceRaster.get_data is virtual pure')

if sys.version_info < (3, 6):
    # https://www.python.org/dev/peps/pep-0487/
    for k, v in ASourceRaster.__dict__.items():
        if hasattr(v, '__set_name__'):
            v.__set_name__(ASourceRaster, k)
