#include <cfloat>

#include "caffe2/core/context_gpu.h"
#include "softmax_with_loss_op.h"

namespace caffe2 {

namespace {

__global__ void LabelCrossEntropyKernel(
    const int N, const int D, const float* Pdata, const int* labeldata,
    const float* weights, float* Ydata) {
  CUDA_1D_KERNEL_LOOP(i, N) {
    CUDA_KERNEL_ASSERT(labeldata[i] < D);
    float weight = weights ? weights[i] : 1.0;
    Ydata[i] = -logf(max(Pdata[i * D + labeldata[i]], FLT_MIN)) * weight;
  }
}

__global__ void LabelCrossEntropyGradientKernel(
    const int N, const int D, const float* Pdata, const int* labeldata,
    float* dXdata, const float *weights) {
      if (weights == NULL) {
        CUDA_1D_KERNEL_LOOP(i, N) {
         int idx = i * D + labeldata[i];
         dXdata[idx] = Pdata[idx] - 1.;
       }
     } else {
       CUDA_1D_KERNEL_LOOP(i, N) {
        int idx = i * D + labeldata[i];
        dXdata[idx] = Pdata[idx] - 1.;

        float weight = weights[i];
        for(int d=0; d<D; d++) {
            int idx = i * D + d;
            dXdata[idx] *= weight;
        }
     }
   }
}

__global__ void RowMaxKernel(const int num, const int D, const float* data,
    float* out) {
  CUDA_1D_KERNEL_LOOP(index, num) {
    float maxval = -FLT_MAX;
    for (int d = 0; d < D; ++d) {
      maxval = max(data[index * D + d], maxval);
    }
    out[index] = maxval;
  }
}

__global__ void SpatialSoftmaxKernel(const int num, const int D, const int W, const int H,
      const float* Xdata, float* Pdata) {
  CUDA_1D_KERNEL_LOOP(index, num * W * H) {
    int x = index % W;
    int y = (index / W) % H;
    int i = index / W / H;

    // Subtract max on each cell for numerical reasons
    float max_val = -FLT_MAX;
    for(int c = 0; c < D; ++c) {
      int idx = i * (H * W * D) + c * (H * W) + y * W + x;
      max_val = max(max_val, Xdata[idx]);
    }

    // Exponentiate
    float expsum = 0.0f;
    for(int c = 0; c < D; ++c) {
      int idx = i * (H * W * D) + c * (H * W) + y * W + x;
      float expx = exp(Xdata[idx] - max_val);
      Pdata[idx] = expx;
      expsum += expx;
    }

    // Normalize
    for(int c=0; c<D; ++c) {
      int idx = i * (H * W * D) + c * (H * W) + y * W + x;
      Pdata[idx] /= expsum;
    }
  }
}


#define DONTCARE (-1)

__global__ void SpatialCrossEntropyLossKernel(const int N, const int D, const int W, const int H,
    const float* Pdata, const int* label_data, const float *weights,
      float* loss_data, float* weight_data) {
  CUDA_1D_KERNEL_LOOP(index, N * W * H) {
    int x = index % W;
    int y = (index / W) % H;
    int i = index / W / H;
    const int label = static_cast<int>(label_data[index]);

    if (label != DONTCARE) {
      float weight = (weights == NULL ? 1.0 : weights[index]);
      loss_data[index] = -log(max(
        Pdata[i * W * H * D + label * W * H + y * W + x], 1e-20f)) * weight;
      weight_data[index] = weight;
    } else {
      loss_data[index] = 0;
      weight_data[index] = 0;
    }
  }
}

__global__ void SpatialSoftmaxLossGradientKernel(const int N, const int D,
    const int W, const int H, const int* label_data, const float* weights,
         float* dX_data, float* weights_) {
 CUDA_1D_KERNEL_LOOP(index, N * W * H) {
   int x = index % W;
   int y = (index / W) % H;
   int i = index / W / H;
   const int label = static_cast<int>(label_data[index]);

   if (label != DONTCARE) {
     int data_idx = i * (H * W * D) + label * (H * W) + y * W + x;
     dX_data[data_idx] -= 1.0;
     if (weights != NULL) {
       float weight = weights[index];
       for (int c = 0; c < D; ++c) {
         int data_idx = i * (H * W * D) + c * (H * W) + y * W + x;
         dX_data[data_idx] *= weight;
       }
       weights_[index] = weight;
     } else {
       weights_[index] = 1.0;
     }
   } else {
     // Ignore-label, so set all gradients for this positions
     // tp zero
     for (int c = 0; c < D; ++c) {
       int data_idx = i * (H * W * D) + c * (H * W) + y * W + x;
       dX_data[data_idx] = 0.0;
     }
     weights_[index] = 0.0;
   }
 }
}

__global__ void SoftmaxNormalizeKernel(
    const int nthreads, const int D, const float* Pdata, const float* scales,
    float* out) {
  CUDA_1D_KERNEL_LOOP(index, nthreads) {
    int n = index / D;
    out[index] = Pdata[index] / scales[n];
  }
}

void Softmax(const int N, const int D, const float* logits, const int* labels,
             const float* sum_multiplier, float* scales, float* probs,
             CUDAContext* context) {
  const int size = N * D;
  RowMaxKernel<<<CAFFE_GET_BLOCKS(N), CAFFE_CUDA_NUM_THREADS,
                 0, context->cuda_stream()>>>(N, D, logits, scales);
  // Put the intermediate result X - max(X) into Y
  context->Copy<float, CUDAContext, CUDAContext>(size, logits, probs);
  // Subtract the scale
  math::Gemm<float, CUDAContext>(CblasNoTrans, CblasNoTrans, N, D, 1,
                                 -1, scales, sum_multiplier, 1, probs, context);
  // Exponentiation
  math::Exp<float, CUDAContext>(size, probs, probs, context);
  // Sum exponentiated values
  math::Gemv<float, CUDAContext>(CblasNoTrans, N, D, 1, probs, sum_multiplier,
                                 0, scales, context);
  // Normalize
  SoftmaxNormalizeKernel<<<CAFFE_GET_BLOCKS(size), CAFFE_CUDA_NUM_THREADS,
                           0, context->cuda_stream()>>>(
    size, D, probs, scales, probs);
}

} // namespace

template<>
bool SoftmaxWithLossOp<float, CUDAContext>::RunOnDevice() {
  auto& X = Input(0);  // Logits
  auto& T = Input(1);  // Labels / targets
  auto* P = Output(0); // Probabilities from softmax
  auto* avg_loss = Output(1); // Average loss
  const float* weights = (InputSize() > 2 ? Input(2).data<float>() : NULL);

  int N = X.dim32(0);
  int D = X.dim32(1);
  P->ResizeLike(X);
  total_weight_ptr_.Resize(1);

  if (!spatial_mode_) {
    DCHECK_EQ(X.ndim(), 2);
    DCHECK((T.ndim() == 1) || (T.ndim() == 2 && T.dim32(1) == 1));
    DCHECK_EQ(T.dim32(0), N);

    avg_loss->Resize(vector<TIndex>());
    if (losses_.size() != N) {
      losses_.Resize(N);
    }
    if (sum_multiplier_.size() != D) {
      sum_multiplier_.Resize(D);
      math::Set<float, CUDAContext>(
          D, 1.f, sum_multiplier_.mutable_data<float>(), &context_);
    }
    Softmax(N, D, X.data<float>(), T.data<int>(), sum_multiplier_.data<float>(),
            losses_.mutable_data<float>(), P->mutable_data<float>(), &context_);
    // Compute label xent loss per example
    LabelCrossEntropyKernel<<<CAFFE_GET_BLOCKS(N), CAFFE_CUDA_NUM_THREADS,
                              0, context_.cuda_stream()>>>(
        N, D, P->data<float>(), T.data<int>(), weights,
        losses_.mutable_data<float>());

    float total_weight = N;
    if (weights) {
      // Sum weights
      math::Sum<float, CUDAContext>(N, weights,
        total_weight_ptr_.mutable_data<float>(), &context_);
      cudaMemcpyAsync(&total_weight, total_weight_ptr_.data<float>(),
        sizeof(float), cudaMemcpyDeviceToHost, context_.cuda_stream());
    }

    // Sum of all losses
    float* avg_loss_data = avg_loss->mutable_data<float>();
    math::Sum<float, CUDAContext>(
        losses_.size(), losses_.data<float>(), avg_loss_data, &context_);
    // Average of input batch size
    math::Scale<float, CUDAContext>(
        1, scale_ / total_weight, avg_loss_data, avg_loss_data, &context_);
  } else {
    DCHECK_EQ(X.ndim(), 4);
    DCHECK_EQ(T.ndim(), 3);
    DCHECK_EQ(T.dim32(0), N);

    int H = X.dim32(2);
    int W = X.dim32(3);
    if (losses_.size() != N * W * H) {
      losses_.Resize(N * W * H);
    }
    if (weights_.size() != N * W * H) {
      weights_.Resize(N * W * H);
    }

    const float* Xdata = X.data<float>();
    float* Pdata = P->mutable_data<float>();

    // Softmax for each x,y location
    SpatialSoftmaxKernel<<<CAFFE_GET_BLOCKS(N), CAFFE_CUDA_NUM_THREADS,
                           0, context_.cuda_stream()>>>(
        N, D, W, H, Xdata, Pdata);

    // Cross entropy
    avg_loss->Resize(vector<TIndex>());
    float* avg_loss_data = avg_loss->mutable_data<float>();
    math::Set<float, CUDAContext>(1, 0.0f, avg_loss_data, &context_);

    const int* label_data = T.data<int>();
    math::Set<float, CUDAContext>(
      1, 0.0f, total_weight_ptr_.mutable_data<float>(), &context_);

    SpatialCrossEntropyLossKernel<<<CAFFE_GET_BLOCKS(N * W * H),
      CAFFE_CUDA_NUM_THREADS, 0, context_.cuda_stream()>>>(
        N, D, W, H, P->data<float>(), label_data, weights,
        losses_.mutable_data<float>(), weights_.mutable_data<float>());


    // Somewhat awkward scalar passing from device to host
    float h_total_weight;
    math::Sum<float, CUDAContext>(
      weights_.size(), weights_.data<float>(),
      total_weight_ptr_.mutable_data<float>(), &context_);
    cudaMemcpyAsync(&h_total_weight, total_weight_ptr_.data<float>(),
      sizeof(float), cudaMemcpyDeviceToHost, context_.cuda_stream());

    math::Sum<float, CUDAContext>(
        losses_.size(), losses_.data<float>(), avg_loss_data, &context_);

    // Final scaling
    if (h_total_weight > 0) {
      math::Scale<float, CUDAContext>(
          1, scale_ / h_total_weight,
          avg_loss_data, avg_loss_data, &context_);
    }
  }
  return true;
}


template<>
bool SoftmaxWithLossGradientOp<float, CUDAContext>::RunOnDevice() {
  auto& X = Input(0);  // Logits
  auto& T = Input(1);  // Labels / targets
  // Input(2) is weights, if given
  auto& P = Input(InputSize() - 2);  // Probabilities from softmax
  auto& d_avg_loss = Input(InputSize() - 1); // Gradient w.r.t. avg loss
  const float* weights = (InputSize() > 4 ? Input(2).data<float>() : NULL);

  auto* dX = Output(0);
  int N = X.dim32(0);
  int D = X.dim32(1);
  dX->ResizeLike(X);
  total_weight_ptr_.Resize(1);

  if (!spatial_mode_) {
    DCHECK_EQ(X.ndim(), 2);
    DCHECK((T.ndim() == 1) || (T.ndim() == 2 && T.dim32(1) == 1));
    DCHECK_EQ(T.dim32(0), N);
    // Copy softmax probabilities into dX
    context_.Copy<float, CUDAContext, CUDAContext>(
        P.size(), P.data<float>(), dX->mutable_data<float>());
    // Subtract 1 from labeled positions
    LabelCrossEntropyGradientKernel<<<CAFFE_GET_BLOCKS(N), CAFFE_CUDA_NUM_THREADS,
                                      0, context_.cuda_stream()>>>(
        N, D, P.data<float>(), T.data<int>(), dX->mutable_data<float>(),
        weights);

    float total_weight = N;
    if (weights) {
      // Sum weights
      math::Sum<float, CUDAContext>(
        N, weights, total_weight_ptr_.mutable_data<float>(), &context_);
      cudaMemcpyAsync(&total_weight, total_weight_ptr_.data<float>(),
        sizeof(float), cudaMemcpyDeviceToHost, context_.cuda_stream());
    }

    // Scale by d_avg_loss / N
    math::Scale<float, CUDAContext>(
        dX->size(), scale_ / total_weight, dX->data<float>(),
        dX->mutable_data<float>(), &context_);
    math::Scale<float, CUDAContext>(
        dX->size(), d_avg_loss.data<float>(), dX->data<float>(),
        dX->mutable_data<float>(), &context_);
  } else {
    // Spatial mode, compute softmax for each x, y location
    DCHECK_EQ(X.ndim(), 4);
    DCHECK_EQ(T.ndim(), 3);

    int H = X.dim32(2);
    int W = X.dim32(3);
    dX->ResizeLike(X);
    if (weights_.size() != N * W * H) {
      weights_.Resize(N * W * H);
    }

    const float* Pdata = P.data<float>();
    float* dX_data = dX->mutable_data<float>();
    const int* label_data = T.data<int>();
    const float* d_avg_loss_data = d_avg_loss.data<float>();

    // Copy softmax probabilities into dX. All but the neuron
    // corresponding to the correct label has gradient equaling e(x_j)
    // which is the probability under softmax.
    context_.Copy<float, CUDAContext, CUDAContext>(P.size(), Pdata, dX_data);

    math::Set<float, CUDAContext>(
      1, 0.0f, total_weight_ptr_.mutable_data<float>(), &context_);

    SpatialSoftmaxLossGradientKernel<<<CAFFE_GET_BLOCKS(N * W * H),
      CAFFE_CUDA_NUM_THREADS, 0, context_.cuda_stream()>>>(
        N, D, W, H, label_data, weights, dX_data,
        weights_.mutable_data<float>());

    math::Sum<float, CUDAContext>(
      weights_.size(), weights_.data<float>(),
      total_weight_ptr_.mutable_data<float>(), &context_);

    // Somewhat awkward scalar passing from device to host
    float h_total_weight;
    cudaMemcpyAsync(&h_total_weight, total_weight_ptr_.data<float>(),
      sizeof(float), cudaMemcpyDeviceToHost, context_.cuda_stream());

    // Final scaling
    if (h_total_weight > 0) {
      math::Scale<float, CUDAContext>(
          dX->size(),
          scale_ / h_total_weight,
          dX->data<float>(),
          dX->mutable_data<float>(),
          &context_);
    }
    math::Scale<float, CUDAContext>(
        dX->size(),
        d_avg_loss.data<float>(),
        dX->data<float>(),
        dX->mutable_data<float>(),
        &context_);
  }
  return true;
}


namespace {
REGISTER_CUDA_OPERATOR(SoftmaxWithLoss,
                       SoftmaxWithLossOp<float, CUDAContext>);
REGISTER_CUDA_OPERATOR(SoftmaxWithLossGradient,
                       SoftmaxWithLossGradientOp<float, CUDAContext>);
} // namespace
} // namespace caffe2
