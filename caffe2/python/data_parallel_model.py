from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from collections import OrderedDict
import logging

from caffe2.python import model_helper, dyndep, scope, workspace, core, memonger
from caffe2.proto import caffe2_pb2

dyndep.InitOpsLibrary("@/caffe2/caffe2/contrib/nccl:nccl_ops")

log = logging.getLogger("data_parallel_model")
log.setLevel(logging.INFO)


def Parallelize_GPU(
    model_helper_obj,
    input_builder_fun,
    forward_pass_builder_fun,
    param_update_builder_fun,
    devices=range(0, workspace.NumCudaDevices()),
    rendezvous=None,
    net_type='dag',
    broadcast_computed_params=True,
    optimize_gradient_memory=False,
):
    '''
    Function to create a model that can run on many GPUs.
      model_helper_obj: an object of ModelHelperBase, such as CNNModelHelper
      input_builder_fun:
                         Function that adds the input operators
                         Note: Remember to instantiate reader outside of this
                         function so all GPUs share same reader object.
                         Signature:  input_builder_fun(model)
      forward_pass_builder_fun:
                        Function to add the operators to the model.
                        Must return list of loss-blob references that
                        are used to build the gradient.
                        Signature: forward_pass_builder_fun(model)
      param_update_builder_fun:
                        Function that adds operators that are run after
                        gradient update, such as updating the weights and
                        weight decaying. Function is also passed the learning
                        rate scaling factor. You should multiple the learning
                        rate by the factor to maintain invariant of same
                        results with same total batch size, regardless of
                        number of gpus.
                        Signature: param_update_builder_fun(model, lr_scale)
      devices:          List of GPU ids, such as [0, 1, 2, 3],
      rendezvous:       used for rendezvous in distributed computation, if None
                        then only one node is used. To create rendezvous,
                        use <TBD>.
      net_type:         Network type

    '''
    log.info("Parallelizing model for devices: {}".format(devices))
    extra_workers = 8 if rendezvous is not None else 0  # best-guess
    model_helper_obj.net.Proto().num_workers = len(devices) * 4 + extra_workers
    model_helper_obj.net.Proto().type = net_type

    # Store some information in the model -- a bit ugly
    model_helper_obj._devices = devices
    model_helper_obj._rendezvous = rendezvous
    model_helper_obj._grad_names = []

    assert isinstance(model_helper_obj, model_helper.ModelHelperBase)
    assert model_helper_obj.params == [], "Model needs to be empty"

    # Add input and model
    log.info("Create input and model training operators")

    losses_by_gpu = {}
    for device in devices:
        device_opt = core.DeviceOption(caffe2_pb2.CUDA, device)
        with core.DeviceScope(device_opt):
            with core.NameScope("gpu_{}".format(device)):
                log.info("Model for GPU: {}".format(device))
                input_builder_fun(model_helper_obj)
                losses = forward_pass_builder_fun(model_helper_obj)
                # Losses are not needed for test net
                if param_update_builder_fun is not None:
                    assert isinstance(losses, list), \
                        'Model builder function must return list of loss blobs'
                    for loss in losses:
                        assert isinstance(loss, core.BlobReference), \
                            'Model builder func must return list of loss blobs'

                losses_by_gpu[device] = losses

    # Create parameter map
    model_helper_obj._device_grouped_blobs =\
        _GroupByDevice(devices, model_helper_obj.params)

    # computed params
    computed_params_grouped =\
        _GroupByDevice(devices, model_helper_obj.computed_params)
    model_helper_obj._device_grouped_blobs.update(computed_params_grouped)

    model_helper_obj._param_names =\
        model_helper_obj._device_grouped_blobs.keys()
    model_helper_obj._computed_param_names = computed_params_grouped.keys()

    if (param_update_builder_fun is None):
        log.info("Parameter update function not defined --> only forward")
        return

    log.info("Adding gradient operators")
    _AddGradientOperators(devices, model_helper_obj, losses_by_gpu)

    # Group gradients by device and register to blob lookup
    param_to_grad = model_helper_obj.param_to_grad
    grads_ordered = [param_to_grad[p] for p in
                     model_helper_obj.params if p in param_to_grad]
    gradients_grouped = _GroupByDevice(
        devices,
        grads_ordered,
    )
    model_helper_obj._device_grouped_blobs.update(gradients_grouped)
    model_helper_obj._grad_names = gradients_grouped.keys()

    log.info("Add gradient all-reduces for SyncSGD")
    if broadcast_computed_params:
        _BroadcastComputedParams(devices, model_helper_obj, rendezvous)
    _AllReduceGradients(
        devices, model_helper_obj, rendezvous
    )

    log.info("Post-iteration operators for updating params")
    num_shards = 1 if rendezvous is None else rendezvous['num_shards']
    lr_scale = 1.0 / (len(devices) * num_shards)
    for device in devices:
        device_opt = core.DeviceOption(caffe2_pb2.CUDA, device)
        with core.DeviceScope(device_opt):
            with core.NameScope("gpu_{}".format(device)):
                param_update_builder_fun(model_helper_obj, lr_scale)

    _AnalyzeOperators(model_helper_obj)

    # Configure dagnet to run with only one worker on the first iteration,
    # to prevent concurrency problems with allocs and nccl.
    arg = model_helper_obj.Proto().arg.add()
    arg.name = "first_iter_only_one_worker"
    arg.i = 1

    # Add initial parameter syncs
    log.info("Add initial parameter sync")
    if (rendezvous is not None):
        _AddDistributedParameterSync(
            devices,
            model_helper_obj,
            model_helper_obj.param_init_net,
            model_helper_obj.param_init_net,
            rendezvous,
        )

    _SyncParams(devices, model_helper_obj, model_helper_obj.param_init_net)

    if optimize_gradient_memory:
        _OptimizeGradientMemory(model_helper_obj, losses_by_gpu, devices)


def _AddGradientOperators(devices, model, losses_by_gpu):
        def create_grad(lossp):
            return model.ConstantFill(lossp, str(lossp) + "_grad", value=1.0)

        loss_grad = {}
        # Explicitly need to create gradients on each GPU
        for gpu_id in devices:
            device = core.DeviceOption(caffe2_pb2.CUDA, gpu_id)
            with core.DeviceScope(device):
                for l in losses_by_gpu[gpu_id]:
                    lg = create_grad(l)
                    loss_grad[str(l)] = str(lg)

        model.AddGradientOperators(loss_grad)


def FinalizeAfterCheckpoint(model, blobs, sync_iter=True):
    if not hasattr(model, "_checkpoint_net"):
        uniq_blob_names = [stripParamName(p) for p in blobs]

        # Synchronize to the blob lookup map, as the provided
        # blobs might have non-parameters, such as momemtum blobs.
        log.info("Creating checkpoint synchronization net")
        devices = model.GetDevices()
        for name in uniq_blob_names:
            if name not in model._device_grouped_blobs:
                grouped = {
                    d:
                    core.BlobReference("gpu_{}{}{}".format(
                        d,
                        scope._NAMESCOPE_SEPARATOR,
                        name)
                    ) for d in devices}
                model._device_grouped_blobs[name] = grouped

        model._checkpoint_net = core.Net("checkpoint_sync_net")
        model._checkpoint_net.RunAllOnGPU()

        if (model._rendezvous is not None):
            checkpoint_init_net = core.Net("checkpoint_init_net")
            checkpoint_init_net.RunAllOnGPU()
            _AddDistributedParameterSync(
                devices,
                model,
                checkpoint_init_net,
                model._checkpoint_net,
                model._rendezvous,
                uniq_blob_names,
            )
            workspace.RunNetOnce(checkpoint_init_net)

        # Setup sync of initial params
        _SyncParams(devices, model, model._checkpoint_net, uniq_blob_names)

        # Sync ITER -- which is in CPU scope
        if sync_iter:
            with core.DeviceScope(core.DeviceOption(caffe2_pb2.CPU)):
                for gpu_idx in devices[1:]:
                    model._checkpoint_net.Copy(
                        "gpu_{}/ITER".format(devices[0]),
                        "gpu_{}/ITER".format(gpu_idx),
                    )
        workspace.CreateNet(model._checkpoint_net)

    # Run the sync
    log.info("Run checkpoint net")
    workspace.RunNet(model._checkpoint_net.Proto().name)


def _Broadcast(devices, model, net, param):
    # TODO(akyrola): replace with NCCLBroadcast when it's working
    # Copy params from gpu_0 to other
    master_gpu = devices[0]
    for gpu_idx in devices[1:]:
        device_opt = core.DeviceOption(caffe2_pb2.CUDA, gpu_idx)
        with core.DeviceScope(device_opt):
            net.Copy(
                model._device_grouped_blobs[param][master_gpu],
                model._device_grouped_blobs[param][gpu_idx]
            )


def _SyncParams(devices, model, net, unique_param_names=None):
    if unique_param_names is None:
        unique_param_names = model._param_names

    for param in unique_param_names:
        _Broadcast(devices, model, net, param)


def _AddDistributedParameterSync(
    devices,
    model,
    init_net,
    net,
    rendezvous,
    uniq_param_names=None,
):
    if uniq_param_names is None:
        uniq_param_names = model._param_names

    device_opt = core.DeviceOption(caffe2_pb2.CUDA, devices[0])

    # ITER is in CPU scope :(
    with core.DeviceScope(core.DeviceOption(caffe2_pb2.CPU)):
        comm_world = init_net.CreateCommonWorld(
            rendezvous['kv_handler'],
            "iter_cw",
            name=net.Proto().name + ".iter_cw_op",
            size=rendezvous['num_shards'],
            rank=rendezvous['shard_id'],
            engine=rendezvous['engine'],
        )
        net.Broadcast(
            inputs=[comm_world, "gpu_{}/ITER".format(devices[0])],
            outputs=["gpu_{}/ITER".format(devices[0])],
            engine=rendezvous['engine'],
        )

    for param_name in sorted(uniq_param_names):
        param = model._device_grouped_blobs[param_name][devices[0]]

        with core.DeviceScope(device_opt):
            param_cpu = net.CopyGPUToCPU(param, str(param) + "cpu")

        with core.DeviceScope(core.DeviceOption(caffe2_pb2.CPU)):
            comm_world = init_net.CreateCommonWorld(
                rendezvous['kv_handler'],
                "{}_cw".format(param_name),
                name=net.Proto().name + ".{}_cw_op".format(param_name),
                size=rendezvous['num_shards'],
                rank=rendezvous['shard_id'],
                engine=rendezvous['engine'],
            )

            # Temp: copy to CPU
            net.Broadcast(
                inputs=[comm_world, param_cpu],
                outputs=[param_cpu],
                engine=rendezvous['engine'],
            )
        with core.DeviceScope(device_opt):
            net.CopyCPUToGPU(param_cpu, param)


def _AllReduceGradients(devices, model, rendezvous):
    if rendezvous is None:
        _AllReduceGradientsSingleHost(devices, model)
    else:
        _AllReduceGradientsDistributed(devices, model, rendezvous)


def _AllReduceGradientsDistributed(
    devices,
    model,
    rendezvous,
):
    num_workers = model.net.Proto().num_workers
    assert num_workers > 1, "Please specify more than 1 worker"
    all_reduce_engine = rendezvous['engine']

    # Make list of gradients in reverse order
    reverse_ordered_grads = _GetReverseOrderedGrads(model)

    # Step 1: sum gradients from local GPUs to master GPU
    master_device_opt = core.DeviceOption(caffe2_pb2.CUDA, devices[0])
    reducing_device_opt = master_device_opt
    if all_reduce_engine == "RDMA_TCP":
        reducing_device_opt = core.DeviceOption(caffe2_pb2.CPU, 0)

    # We need to specify a partial order using control_input to
    # ensure progress (since all machines need to do same all reduces
    # in parallel)
    num_controls = min(4, num_workers - 1)

    cyclical_controls = []
    counter = 0
    nccl_control_blob = None

    # Note: sorted order to ensure each host puts the operators in
    # same order.
    for grad_name in reverse_ordered_grads:
        master_grad = model._device_grouped_blobs[grad_name][devices[0]]
        grads_group = model._device_grouped_blobs[grad_name].values()

        assert master_grad in grads_group

        # Remark: NCCLReduce does not support in-place modifications
        # so we need a temporary gradient blob
        reduced_grad = str(master_grad) + "_red"

        with core.DeviceScope(master_device_opt):
            model.ConstantFill(master_grad, reduced_grad, value=0.0)

            # Temp fix since NCCLReduce does not work
            model.net.NCCLAllreduce(
                grads_group,
                grads_group,
                control_input=nccl_control_blob,
            )
            nccl_control_blob = grads_group[0]
            model.net.Copy(master_grad, reduced_grad)

        # RDMA_TCP works only on CPU context, so we need a temporary
        # cpu-bound scratch blob.
        if all_reduce_engine == "RDMA_TCP":
            with core.DeviceScope(reducing_device_opt):
                model.param_init_net.ConstantFill(
                    [], reduced_grad + "cpu", shape=[1], value=0.0
                )
            with core.DeviceScope(master_device_opt):
                # Hack to ensure the cpu-scratch blob is initialized
                # prior to running the net.
                model.param_init_net.CopyGPUToCPU(
                    str(master_grad).replace("_grad", ""), reduced_grad + "cpu"
                )
                model.net.CopyGPUToCPU(reduced_grad, reduced_grad + "cpu")
                reduced_grad = reduced_grad + "cpu"

        control_input = None if len(cyclical_controls) < num_controls \
                        else cyclical_controls[counter % num_controls]

        with core.DeviceScope(reducing_device_opt):
            # Step 2: allreduce between all hosts, between master GPUs
            comm_world = model.param_init_net.CreateCommonWorld(
                rendezvous['kv_handler'],
                "{}_cw".format(grad_name),
                name="{}_cw_op".format(grad_name),
                size=rendezvous['num_shards'],
                rank=rendezvous['shard_id'],
                engine=rendezvous['engine'],
            )
            model.net.Allreduce(
                inputs=[comm_world, reduced_grad],
                outputs=[reduced_grad],
                engine=all_reduce_engine,
                control_input=control_input,
            )

        if reducing_device_opt != master_device_opt:
            with core.DeviceScope(master_device_opt):
                model.net.CopyCPUToGPU(reduced_grad, master_grad)
        else:
            with core.DeviceScope(master_device_opt):
                model.net.Copy(reduced_grad, master_grad)

        if len(cyclical_controls) < num_controls:
            cyclical_controls.append(reduced_grad)
        else:
            cyclical_controls[counter % num_controls] = reduced_grad

        counter += 1

        # Step 3: broadcast locally
        _Broadcast(devices, model, model.net, grad_name)


def _AllReduceGradientsSingleHost(devices, model):
    """Performs NCCL AllReduce to distribute gradients to all the GPUs."""

    if len(devices) == 1:
        return

    # Gradients in reverse order
    reverse_ordered_grads = _GetReverseOrderedGrads(model)

    # Now we need to Allreduce gradients on all the GPUs.
    # Pick GPU #0 as a master GPU.
    master_device_opt = core.DeviceOption(caffe2_pb2.CUDA, devices[0])
    last_out = None

    for grad_name in reverse_ordered_grads:
        with core.DeviceScope(master_device_opt):
            # Group by grads for reduce.
            grads_group = model._device_grouped_blobs[grad_name].values()
            assert len(grads_group) == len(devices), \
                "Each GPU from {}, should have a copy of {}.".format(
                    devices, grad_name)
            model.NCCLAllreduce(
                grads_group,
                grads_group,
                control_input=last_out,
            )
            # last_out is used to serialize the execution of nccls
            last_out = grads_group[0]


def _BroadcastComputedParams(devices, model, rendezvous):
    if rendezvous is None:
        _BroadcastComputedParamsSingleHost(devices, model)
    else:
        _BroadcastComputedParamsDistributed(devices, model, rendezvous)


def _BroadcastComputedParamsDistributed(
    devices,
    model,
    rendezvous,
):
    _BroadcastComputedParamsSingleHost(devices, model)
    log.warn("Distributed computed params all-reduce not implemented yet")


def _BroadcastComputedParamsSingleHost(devices, model):
    '''
    Average computed params over all devices
    '''
    if len(devices) == 1:
        return

    for param_name in model._computed_param_names:
        # Copy from master to others -- averaging would be perhaps better,
        # but currently NCCLAllReduce is too prone to deadlock
        _Broadcast(devices, model, model.net, param_name)


def _GetReverseOrderedGrads(model):
    '''
    Returns the gradients in reverse order (namespace stripped),
    for the optimal synchronization order.
    '''
    return list(reversed(model._grad_names))


# A helper function to extract a parameter's name
def stripParamName(param):
    # Format is "a/b/c/d" -> d
    name = str(param)
    sep = scope._NAMESCOPE_SEPARATOR
    return name[name.rindex(sep) + 1:]


def _AnalyzeOperators(model):
    '''
    Look at all the operators and check that they do not cross device scopes
    '''
    for op in model.Proto().op:
        if "NCCL" in op.type or "Copy" in op.type:
            continue
        op_dev = op.device_option
        op_gpu = op_dev.cuda_gpu_id

        # This avoids failing on operators that are only for CPU
        if op_dev.device_type == caffe2_pb2.CPU:
            continue

        namescope = "gpu_{}/".format(op_gpu)
        for inp in list(op.input) + list(op.output):
            if inp.startswith("gpu_") and not inp.startswith(namescope):
                raise Exception(
                    "Blob {} of op {}, should have namescope {}. Op: {}".format(
                        inp, op.type, "gpu_{}/".format(op_gpu), str(op),
                    ))


def _GroupByDevice(devices, params):
    '''
    Groups blobs by device, returning a map of [blobname] = {0: BlobRef, 1: ..}.
    Returns ordered dictionary, ensuring the original order.
    '''
    grouped = OrderedDict()
    assert len(params) % len(devices) == 0,\
           "There should be equal number of params per device"

    num_params_per_device = int(len(params) / len(devices))

    for i, p in enumerate(params):
        assert isinstance(p, core.BlobReference), \
            "Param {} is not of type BlobReference".format(p)

        name = stripParamName(p)
        gpuid = devices[i // num_params_per_device]
        assert "gpu_{}/".format(gpuid) in p.GetNameScope(),\
            "Param {} expected to have namescope 'gpu_{}'".format(str(p), gpuid)

        if name not in grouped:
            grouped[name] = {}
        grouped[name][gpuid] = p

    # Confirm consistency
    for j, (p, ps) in enumerate(grouped.items()):
        assert \
            len(ps) == len(devices), \
            "Param {} does not have value for each device (only {}: {})".format(
                p, len(ps), ps,
            )
        # Ensure ordering
        assert(ps[devices[0]] == params[j])

    return grouped


def _OptimizeGradientMemory(model, losses_by_gpu, devices):
    for device in devices:
        namescope = "gpu_{}/".format(device)
        model.net._net = memonger.share_grad_blobs(
            model.net,
            losses_by_gpu[device],
            set(model.param_to_grad.values()),
            namescope,
        )
