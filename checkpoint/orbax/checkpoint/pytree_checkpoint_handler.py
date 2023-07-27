# Copyright 2023 The Orbax Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""PyTreeCheckpointHandler class.

Implementation of CheckpointHandler interface.
"""

import asyncio
import dataclasses
import re
import typing
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from absl import logging
from etils import epath
import jax
from jax.experimental.array_serialization import serialization
import numpy as np
from orbax.checkpoint import aggregate_handlers
from orbax.checkpoint import transform_utils
from orbax.checkpoint import type_handlers
from orbax.checkpoint import utils
from orbax.checkpoint.async_checkpoint_handler import AsyncCheckpointHandler
from orbax.checkpoint.future import Future
import tensorstore as ts


PyTree = Any
TupleKey = Tuple[str, ...]
RestoreArgs = type_handlers.RestoreArgs
ArrayRestoreArgs = type_handlers.ArrayRestoreArgs
SaveArgs = type_handlers.SaveArgs
ParamInfo = type_handlers.ParamInfo
TypeHandler = type_handlers.TypeHandler
AggregateHandler = aggregate_handlers.AggregateHandler
MsgpackHandler = aggregate_handlers.MsgpackHandler
TransformFn = Callable[[PyTree, PyTree, PyTree], Tuple[PyTree, PyTree]]
Transform = transform_utils.Transform
RestoreTransform = transform_utils.RestoreTransform

_TYPE_METADATA_FILE = 'type_metadata'
_CHECKPOINT_FILE = 'checkpoint'


async def _create_param_save_dir(param_info: ParamInfo, args: SaveArgs):
  # Directory will be unused.
  path = param_info.path
  if path is None or args.aggregate:
    return
  if jax.process_index() == 0:
    # TODO(b/273803615): Note that keys with slashes ('/', generated by Haiku,
    # for example) will result in the creation of nested sub-directories, rather
    # than flat parameter directories like for a standard neste PyTree. This
    # discrepancy, while potentially problematic, will not be addressed since we
    # anticipate moving fully to OCDBT within a quarter or two.
    await utils.async_makedirs(path, parents=True)


def _maybe_set_default_save_args(value, args):
  # If already set, return.
  if isinstance(args, SaveArgs):
    return args
  aggregate = not type_handlers.has_type_handler(type(value))
  return SaveArgs(aggregate=aggregate)


def _maybe_set_default_restore_args(args):
  if isinstance(args, RestoreArgs):
    return args
  return RestoreArgs(restore_type=None)


def _try_array_cast(arr, dtype):
  if dtype is not None:
    if utils.is_scalar(arr):
      arr = np.asarray(arr).astype(dtype).item()
    else:
      if hasattr(arr, 'astype'):
        arr = arr.astype(dtype)
  return arr


def _maybe_shard_array(value, args):
  if hasattr(value, 'reshape') and isinstance(args, ArrayRestoreArgs):
    value = value.reshape(args.global_shape)
    sharding = args.sharding or jax.sharding.NamedSharding(
        args.mesh, args.mesh_axes
    )
    value = jax.make_array_from_callback(
        value.shape, sharding, lambda idx: value[idx]
    )
  return value


def _get_param_names(item: PyTree) -> PyTree:
  """Gets parameter names for PyTree elements."""
  def _param_name_from_keypath(keypath: Tuple[Any, ...]) -> str:
    return '.'.join([str(utils.get_key_name(k)) for k in keypath])

  return jax.tree_util.tree_map_with_path(
      lambda kp, _: _param_name_from_keypath(kp),
      item,
      is_leaf=utils.is_empty_or_leaf,
  )


def _keystr(key: Tuple[Any, ...]) -> str:
  return '/'.join(key)


def _find_matching_input_args(
    input_key: TupleKey,
    flat_item: Dict[TupleKey, Any],
    flat_transforms: Dict[TupleKey, Transform],
    flat_restore_args: Dict[TupleKey, RestoreArgs],
) -> Optional[RestoreArgs]:
  """Given an input_key, tries to find matching RestoreArgs for the input.
  
  Args:
    input_key: A key in the input tree.
    flat_item: The flattened, user-provided item.
    flat_transforms: Flattened transformations dict.
    flat_restore_args: Flattened tree of RestoreArgs, relative to item.
    
  Returns:
    RestoreArgs that match the given input_key, according to the
    transformations, or None if no match is found.
  """
  for transform_key, transform in flat_transforms.items():
    if transform.multi_value_fn is not None:
      if not isinstance(transform, RestoreTransform):
        raise ValueError(
            'Must use RestoreTransform in order to use multi_value_fn'
            ' during restore.'
        )
      if transform.multi_value_fn_input_args is None:
        raise ValueError(
            '`multi_value_fn` was specified, but'
            ' `multi_value_fn_input_args` were not. The latter must be'
            ' specified to identify inputs for the function.'
        )
      for (
          input_key_regex,
          input_args,
      ) in transform.multi_value_fn_input_args.items():
        if re.fullmatch(input_key_regex, _keystr(input_key)):
          return input_args
    elif not transform.use_fallback:
      # The following is done to reverse-engineer the regex for the key in
      # the original tree.
      for output_key in flat_item:
        match = re.fullmatch(_keystr(transform_key), _keystr(output_key))
        if match:
          if transform.original_key is None:
            # If transform.original_key is not specified, this transform
            # does not rename the original key. We can reuse the key from
            # the item.
            input_key_pattern = _keystr(output_key)
          else:
            input_key_pattern = match.expand(transform.original_key)
          if input_key_pattern == _keystr(input_key):
            return flat_restore_args[output_key]
  return None


def _has_use_fallback_transform(
    input_key: TupleKey, flat_transforms: Dict[TupleKey, Transform]
) -> bool:
  result = False
  for transform_key, transform in flat_transforms.items():
    match = re.fullmatch(_keystr(transform_key), _keystr(input_key))
    if match and transform.use_fallback:
      result = True
  return result


def _get_restore_parameters(
    directory: epath.Path,
    item: Optional[PyTree],
    structure: PyTree,
    transforms: Optional[PyTree],
    restore_args: Optional[PyTree],
    byte_limiter: Optional[serialization._LimitInFlightBytes] = None,
    transforms_default_to_original: bool = True,
) -> Tuple[PyTree, PyTree]:
  """Construct parameters needed for restoration.

  If transforms are not provided, the method is pretty simple: param_infos are
  constructed from the structure of the original checkpoint, and restore_args
  are serialized to a tree structure compatible with param_infos and structure.

  If transforms are provided, things become more complicated because we must
  determine exactly which parameters the user desires to restore, and construct
  param_infos and restore_args for these, while discarding unneeded parameters.
  In essence, the process can be thought of as reversing the transformations.
  This happens differently for different types of transforms.
  1. Renamed key: Identify the original key name (in the checkpoint) and carry
    over the provided restore args for the parameter.
  2. multi_value_fn: Users are required to specify multi_value_fn_input_args.
    Any keys named here must be loaded, and their restore args are also given
    here.
  3. Unspecified key: A key which is unspecified in the transforms but present
    in the `item` is a key that is carried over from the checkpoint unchanged.
  4. Fallback key: This is a key that is present in the `item` but not in the
    original checkpoint. It does not need to be restored.
  5. Keys present in the original checkpoint but not in the `item`/`transforms`
    are implicitly ignored, and not restored.

  Args:
    directory: Checkpoint directory.
    item: Optional reference item.
    structure: The structure of the original checkpoint.
    transforms: User-provided transformations. If None, they were not provided.
      Has the structure of the desired output tree.
    restore_args: User-provided restoration arguments. If None, they were not
      provided. Otherwise, the tree has the same structure as the desired output
      tree.
    byte_limiter: A _LimitInFlightBytes object.
    transforms_default_to_original: See transform_utils.apply_transformations.

  Returns:
    Tuple of param_infos, and restore_args.
  """
  flat_structure = utils.to_flat_dict(structure, keep_empty_nodes=True)
  if restore_args is None:
    restore_args = jax.tree_util.tree_map(lambda x: RestoreArgs(), structure)
  flat_restore_args = utils.to_flat_dict(restore_args, keep_empty_nodes=True)
  flat_param_infos = {}
  flat_input_restore_args = {}
  is_ocdbt_checkpoint = type_handlers.is_ocdbt_checkpoint(directory)

  def _get_param_info(nested_name: Tuple[str, ...], leaf: Any) -> ParamInfo:
    if utils.leaf_is_placeholder(leaf):
      # Leaf is a param name.
      path = directory / utils.name_from_leaf_placeholder(leaf)
    # The following is kept for backwards compatibility.
    elif isinstance(leaf, ts.Spec):
      tspec = leaf.to_json()  # pytype: disable=attribute-error
      # Skip '.', since we need special regex handling for this char.
      pattern = r'\.' + utils.TMP_DIR_SUFFIX[1:] + r'\d+'
      path = re.sub(pattern, '', tspec['kvstore']['path'])
    elif utils.is_supported_empty_aggregation_type(leaf):
      return leaf  # Empty node, ParamInfo should not be returned.
    elif utils.is_supported_aggregation_type(leaf):
      # Value already restored, do not need ts.Spec.
      path = None
    else:
      raise ValueError(f'Unsupported type: {type(leaf)}')
    return ParamInfo(
        name='.'.join(nested_name),
        path=path,
        skip_deserialize=(path is None),
        is_ocdbt_checkpoint=is_ocdbt_checkpoint,
        byte_limiter=byte_limiter,
    )

  if transforms is None:
    for key, value in flat_structure.items():
      flat_param_infos[key] = _get_param_info(key, value)
    restore_args = utils.serialize_tree(restore_args, keep_empty_nodes=True)
  else:
    if item is None:
      raise ValueError(
          'If providing `transforms`, must provide `item` matching structure'
          ' of expected result.'
      )
    flat_item = utils.to_flat_dict(item, keep_empty_nodes=True)
    flat_transforms = utils.to_flat_dict(transforms)

    for input_key, value in flat_structure.items():
      maybe_input_args = _find_matching_input_args(
          input_key, flat_item, flat_transforms, flat_restore_args
      )
      if maybe_input_args:
        flat_param_infos[input_key] = _get_param_info(input_key, value)
        flat_input_restore_args[input_key] = maybe_input_args
      elif input_key in flat_item and input_key in flat_structure:
        # Key is present in both input and output.
        if _has_use_fallback_transform(input_key, flat_transforms):
          # Indicates that a `use_fallback` transformation was specified.
          if transforms_default_to_original:
            # Specified `use_fallback`, but key was also present in the
            # checkpoint. This means we should skip loading, since it will be
            # overridden with a new value.
            flat_param_infos[input_key] = ParamInfo(skip_deserialize=True)
            flat_input_restore_args[input_key] = RestoreArgs()
          else:
            # Specified `use_fallback`, but `transforms_default_to_original`
            # is False. This means we draw the value from the user-provided
            # `item`.
            flat_param_infos[input_key] = _get_param_info(input_key, value)
            flat_input_restore_args[input_key] = flat_restore_args[input_key]
        else:
          # Transform not specified.
          if transforms_default_to_original:
            # Key/value is carried over from the original unchanged.
            flat_param_infos[input_key] = _get_param_info(input_key, value)
            flat_input_restore_args[input_key] = flat_restore_args[input_key]
          else:
            # Take the value from the user-provided `item`, ignoring any value
            # in the checkpoint.
            flat_param_infos[input_key] = ParamInfo(skip_deserialize=True)
            flat_input_restore_args[input_key] = RestoreArgs()
      else:
        # No match, restoration not required since it will be dropped from the
        # output.
        flat_param_infos[input_key] = ParamInfo(skip_deserialize=True)
        flat_input_restore_args[input_key] = RestoreArgs()

    restore_args = utils.from_flat_dict(
        flat_input_restore_args, target=structure
    )

  return (
      utils.from_flat_dict(flat_param_infos, target=structure),
      restore_args,
  )


def _get_tree_for_aggregation(param_infos, save_args, item):
  """Get tree for aggregated checkpoint."""

  # TODO(b/283164080): These type checks result in logic from the lower layer
  # (TypeHandler/AggregateHandler) leaking into the upper layer
  # (CheckpointHandler). Ideally, AggregateHandler could define its own
  # supported values and error conditions.
  def _get_leaf_for_aggregation(param_info, arg, value):
    if arg.aggregate:  # Param was aggregated, return value after cast.
      if isinstance(value, jax.Array) and not value.is_fully_replicated:
        raise ValueError(
            'jax.Array must be fully replicated to be saved in aggregate file.'
        )
      if not utils.is_supported_aggregation_type(value):
        # Not an error because users' training states often have a bunch of
        # random unserializable objects in them (empty states, optimizer
        # objects, etc.).
        value = None
      return _try_array_cast(value, arg.dtype)
    else:  # Placeholder string for non-aggregated value.
      return utils.leaf_placeholder(param_info.name)

  return jax.tree_util.tree_map(
      _get_leaf_for_aggregation, param_infos, save_args, item
  )


@dataclasses.dataclass
class _BatchRequest:
  """Represents a a request for batched serialization or deserialization."""
  handler: TypeHandler
  values: List[Any]
  infos: List[ParamInfo]
  args: List[Union[SaveArgs, RestoreArgs]]


def _batched_serialization_requests(
    tree: PyTree, param_infos: PyTree, args: PyTree
) -> List[_BatchRequest]:
  """Gets a list of batched serialization or deserialization requests."""
  result = []
  grouped = {}

  def _group_value(info, value, arg):
    nonlocal result
    nonlocal grouped
    # Exclude from serialize/deserialize with TypeHandler if aggregated.
    if info.skip_deserialize:
      return
    if isinstance(arg, RestoreArgs):
      handler = type_handlers.get_type_handler(arg.restore_type)
    else:
      handler = type_handlers.get_type_handler(type(value))
    if handler not in grouped:
      grouped[handler] = _BatchRequest(handler, [], [], [])
    request = grouped[handler]
    grouped[handler] = dataclasses.replace(
        request,
        values=request.values + [value],
        infos=request.infos + [info],
        args=request.args + [arg],
    )

  jax.tree_util.tree_map(
      _group_value,
      param_infos,
      tree,
      args,
  )
  return result + list(grouped.values())


def _multi_value_fns_with_args(
    transforms: PyTree, restore_args: PyTree
) -> PyTree:
  """Constructs a wrapper for multi_value_fn including RestoreArgs."""
  flat_restore_args = utils.to_flat_dict(restore_args, sep='/')

  def _maybe_wrap_transform(transform: Transform):
    def _multi_value_fn_with_args(transform_key: str, tree: PyTree) -> Any:
      nonlocal transform
      transform = typing.cast(RestoreTransform, transform)
      return transform.multi_value_fn(
          transform_key, tree, flat_restore_args[transform_key]
      )

    if transform.multi_value_fn is not None:
      return Transform(multi_value_fn=_multi_value_fn_with_args)
    else:
      return transform

  return jax.tree_util.tree_map(_maybe_wrap_transform, transforms)


def _transform_structure(
    item: PyTree,
    restored: PyTree,
    restore_args: Optional[PyTree],
    transforms: Optional[PyTree],
    transforms_default_to_original: bool,
) -> PyTree:
  """Optionally transforms the restored PyTree to the structure of `item`.

  Args:
    item: a PyTree representing the result structure ("new tree structure").
    restored: a PyTree representing the original tree structure.
    restore_args: tree of RestoreArgs, with the same structure as `item`.
    transforms: provides instructions on how to transform the input trees. See
      transform_utils.
    transforms_default_to_original: See transform_utils.

  Returns:
    A transformed PyTree.
  """
  if item is None:
    if transforms is not None:
      msg = ('If providing `transforms`, must provide `item` matching structure'
             ' of expected result.')
      raise ValueError(msg)
    item = restored
  else:
    if transforms is None:
      item = utils.deserialize_tree(restored, item)
    else:
      if restore_args is None:
        raise ValueError(
            'If providing `transforms`, must provide `restore_args` matching'
            ' structure of expected result.'
        )
      transforms = _multi_value_fns_with_args(transforms, restore_args)
      item = transform_utils.apply_transformations(
          restored, transforms, item, transforms_default_to_original)
  return item


class PyTreeCheckpointHandler(AsyncCheckpointHandler):
  """A CheckpointHandler implementation for any PyTree structure.

  The PyTree is assumed to be a nested dictionary with array values represented
  as array-like objects (see type_handlers for supported objects). If not
  `jax.Array`, arrays are expected to be fully replicated.
  """

  def __init__(
      self,
      aggregate_filename: Optional[str] = None,
      concurrent_gb: int = 96,
      use_ocdbt: bool = False,
      restore_with_serialized_types: bool = True,
  ):
    """Creates PyTreeCheckpointHandler.

    Args:
      aggregate_filename: name that the aggregated checkpoint should be saved
        as.
      concurrent_gb: max concurrent GB that are allowed to be read.
      use_ocdbt: enables Tensorstore OCDBT driver.
      restore_with_serialized_types: If True, the values with unspecified
        restore types will be restored using the typing information in the
        checkpoint. Otherwise, arrays will be restored as either np.ndarray or
        jax.Array, and will ignore any typing information present in the
        checkpoint.
    """
    self._aggregate_handler = MsgpackHandler()
    if aggregate_filename is None:
      aggregate_filename = _CHECKPOINT_FILE
    self._aggregate_filename = aggregate_filename
    self._concurrent_gb = concurrent_gb
    self._use_ocdbt = use_ocdbt
    self._restore_with_serialized_types = restore_with_serialized_types

  def _get_param_names(self, item: PyTree) -> PyTree:
    """Gets parameter names for PyTree elements."""
    return _get_param_names(item)

  def _get_param_infos(
      self, item: PyTree, directory: epath.Path, save_args: PyTree
  ) -> Tuple[PyTree, bool]:
    """Returns parameter information for elements in `item`.

    At minimum, this method should extract the names of each parameter for
    saving/restoring.

    Args:
      item: a PyTree to extract information from.
      directory: a directory where checkpoint files are located.
      save_args: PyTree matching item containing SaveArgs.

    Returns:
      A PyTree matching `item` of ParamInfo, and a bool indicating whether all
      parameters were aggregated. The bool can enable us to skip some steps
      later, potentially saving time.
    """
    if not item:
      raise ValueError('Found empty item')
    names = self._get_param_names(item)
    all_params_aggregated = True

    def _param_info(name, args):
      nonlocal all_params_aggregated
      all_params_aggregated &= args.aggregate
      return ParamInfo(
          name=name, path=(directory / name), skip_deserialize=args.aggregate
      )

    return (
        jax.tree_util.tree_map(_param_info, names, save_args),
        all_params_aggregated,
    )

  async def _write_aggregate_file(
      self,
      directory: epath.Path,
      item: PyTree,
      param_infos: PyTree,
      save_args: PyTree,
  ) -> Future:
    ser_item = _get_tree_for_aggregation(param_infos, save_args, item)
    return await self._aggregate_handler.serialize(
        directory / self._aggregate_filename, ser_item
    )

  async def async_save(
      self,
      directory: epath.Path,
      item: PyTree,
      save_args: Optional[PyTree] = None) -> Optional[List[Future]]:
    """Saves a PyTree from a given training step.

    This operation is compatible with a multi-host, multi-device setting. Tree
    leaf values must be supported by type_handlers. Standard supported types
    include scalars, np.ndarray, jax.Array, string.

    After saving, all files will be located in "directory/".

    Saves an additional file to "directory/checkpoint" on host 0 which
    contains the serialized structure of `item`, along with any parameters that
    request aggregation.

    Args:
      directory: save location directory.
      item: a PyTree to be saved.
      save_args: a PyTree matching `item` which consists of SaveArgs objects as
        values.

    Returns:
      A Future that will commit the data to `directory` when awaited. Copying
      the data from its source will be awaited in this function.
    """
    # Because of empty states, the user-provided args may not contain
    # all necessary arguments. These should be filled in with default args.
    save_args = jax.tree_util.tree_map(
        _maybe_set_default_save_args,
        item,
        item if save_args is None else save_args,
        is_leaf=utils.is_empty_or_leaf,
    )
    param_infos, all_params_aggregated = self._get_param_infos(
        item, directory, save_args
    )
    if not self._use_ocdbt and not all_params_aggregated:
      # Create directories in parallel.
      await asyncio.gather(
          *jax.tree_util.tree_flatten(
              jax.tree_util.tree_map(
                  _create_param_save_dir, param_infos, save_args
              )
          )[0]
      )
      utils.sync_global_devices(
          'PyTreeCheckpointHandler:create_param_save_dirs'
      )

    if all_params_aggregated:
      commit_futures = []
    else:
      serialize_ops = []
      batch_requests = _batched_serialization_requests(
          item, param_infos, save_args
      )
      for request in batch_requests:
        serialize_ops += [
            request.handler.serialize(
                request.values, request.infos, request.args
            )
        ]
      # Await copy futures. Returns list of lists.
      commit_futures = await asyncio.gather(*serialize_ops)
      commit_futures, _ = jax.tree_util.tree_flatten(commit_futures)

    aggregate_commit_future = await self._write_aggregate_file(
        directory, item, param_infos, save_args
    )
    return commit_futures + [aggregate_commit_future]

  def save(self, directory: epath.Path, item: Any, *args, **kwargs):
    """Saves the provided item.

    Blocks until both copy and commit complete.

    See async_save.

    Args:
      directory: the directory to save to.
      item: the item to be saved.
      *args: additional arguments for save.
      **kwargs: additional arguments for save.
    """

    async def async_save(*args, **kwargs):
      commit_futures = await self.async_save(*args, **kwargs)  # pytype: disable=bad-return-type
      # Futures are already running, so sequential waiting is equivalent to
      # concurrent waiting.
      if commit_futures:  # May be None.
        for future in commit_futures:
          future.result()  # Block on result.

    asyncio.run(async_save(directory, item, *args, **kwargs))
    utils.sync_global_devices('PyTreeCheckpointHandler:save')

  async def _maybe_deserialize(
      self, structure: PyTree, param_infos: PyTree, restore_args: PyTree
  ) -> PyTree:
    """Deserializes values or gets them from the aggregate file."""

    # Handle parameters from aggregate file.
    def _process_aggregated_value(info, value, args):
      if info.skip_deserialize:
        value = _try_array_cast(value, args.dtype)
        value = _maybe_shard_array(value, args)
      return value

    structure = jax.tree_util.tree_map(
        _process_aggregated_value, param_infos, structure, restore_args
    )

    batch_requests = _batched_serialization_requests(
        structure, param_infos, restore_args
    )
    deserialized_batches = []
    deserialized_batches_ops = []
    for request in batch_requests:
      deserialized_batches_ops.append(
          request.handler.deserialize(request.infos, request.args)
      )
    deserialized_batches += await asyncio.gather(*deserialized_batches_ops)

    flat_restored = utils.to_flat_dict(structure, sep='.')
    for request, deserialized in zip(batch_requests, deserialized_batches):
      for info, value in zip(request.infos, deserialized):
        flat_restored[info.name] = value

    restored = utils.from_flat_dict(flat_restored, target=structure, sep='.')
    return restored

  def restore(
      self,
      directory: epath.Path,
      item: Optional[PyTree] = None,
      restore_args: Optional[PyTree] = None,
      transforms: Optional[PyTree] = None,
      transforms_default_to_original: bool = True,
      transform_fn: Optional[TransformFn] = None,
  ) -> PyTree:
    """Restores a PyTree from the checkpoint directory at the given step.

    Optional arguments meshes and mesh_axes define how each array in the
    restored tree should be partitioned. For more information, see below and see
    pjit documentation.

    Args:
      directory: save location directory.
      item: provides the structure for the restored item. If not provided, will
        infer the structure from the saved checkpoint. Transformations will not
        be run.
      restore_args: optional object containing additional arguments for
        restoration. It should be a PyTree matching the structure of `item`, and
        should contain a RestoreArgs object for every value. If `item` is not
        provided, should match the structure of the checkpoint.
      transforms: a PyTree of transformations that should be applied to the
        saved item in order to obtain a final structure. See `transform_utils`
        for further information.
      transforms_default_to_original: See transform_utils.apply_transformations.
      transform_fn: WARNING: NOT GENERALLY SUPPORTED. A function which accepts
        the `item` argument, a PyTree checkpoint structure and a PyTree of
        ParamInfos based on the checkpoint. Returns a transformed PyTree
        matching the desired return tree structure, and a matching ParamInfo
        tree.

    Returns:
      A PyTree matching the structure of `item`.

    Raises:
      FileNotFoundError: `directory` does not exist or is missing required files
      ValueError: `transforms` is provided without `item`.
      ValueError: `transforms` contains elements with `multi_value_fn`.
    """
    if not directory.exists():
      raise FileNotFoundError(
          f'Requested directory for restore does not exist at {directory}')

    async def _create_byte_limiter():
      # Wrap creation in async function to avoid issues on python<=3.9.
      concurrent_bytes = self._concurrent_gb * 10**9
      # Construction must take place here so that it is within the same async
      # method, to prevent errors resulting from different event loops, and
      # cannot be created below this level because there must be a single object
      # for the entire restore call.
      return serialization._LimitInFlightBytes(concurrent_bytes)  # pylint: disable=protected-access

    byte_limiter = asyncio.run(_create_byte_limiter())
    structure = self.structure(directory)
    # `checkpoint_restore_args` has a structure relative to the checkpoint,
    # while `restore_args` remains structured relative to the output.
    param_infos, checkpoint_restore_args = _get_restore_parameters(
        directory,
        item,
        structure,
        transforms,
        restore_args,
        byte_limiter=byte_limiter,
        transforms_default_to_original=transforms_default_to_original,
    )

    if transform_fn is not None and transforms is not None:
      raise ValueError('Cannot provide both `transforms` and `transform_fn`.')
    if transform_fn is not None:
      structure, param_infos = transform_fn(item, structure, param_infos)
      if restore_args is None:
        restore_args = jax.tree_util.tree_map(lambda x: RestoreArgs(), item)
      checkpoint_restore_args = restore_args

    restored_item = asyncio.run(
        self._maybe_deserialize(structure, param_infos, checkpoint_restore_args)
    )

    if not transform_fn:
      restored_item = _transform_structure(
          item,
          restored_item,
          restore_args,
          transforms,
          transforms_default_to_original,
      )
    utils.sync_global_devices('PyTreeCheckpointHandler:restore')
    return restored_item

  def structure(self, directory: epath.Path) -> PyTree:
    """Restores the saved PyTree structure without regard for its leaf values.

    Args:
      directory: the directory to restore from.

    Returns:
      The structure of the checkpointed PyTree. Leaves may be of any type.

    Raises:
      FileNotFoundError: if the checkpoint is not found.
    """
    checkpoint_path = directory / self._aggregate_filename
    if checkpoint_path.exists():
      return self._aggregate_handler.deserialize(checkpoint_path)
    else:
      if self._use_ocdbt:
        raise ValueError(
            f'Checkpoint structure file does not exist at {directory}.'
        )
      else:
        logging.error(
            (
                'Checkpoint structure file does not exist at %s.'
                ' Attempting to assume an implicit tree structure.'
            ),
            directory,
        )
        return utils.pytree_structure(directory)

  def close(self):
    """See superclass documentation."""
    self._aggregate_handler.close()
