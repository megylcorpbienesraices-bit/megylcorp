#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SimdBackend { Avx512Ready, Avx2, Scalar }

pub fn detected_backend() -> SimdBackend {
    #[cfg(target_arch="x86_64")]
    {
        if std::is_x86_feature_detected!("avx512f") { return SimdBackend::Avx512Ready; }
        if std::is_x86_feature_detected!("avx2") { return SimdBackend::Avx2; }
    }
    SimdBackend::Scalar
}

/// Validated ITM QUANT GEX convention used by the SIMD dispatcher.  The optimization
/// backend may change, but the math contract does not.
pub fn gex_scalar(gamma:f64, oi:f64, multiplier:f64, spot:f64, dealer_sign:f64)->f64 {
    dealer_sign * gamma * oi * multiplier * spot * spot * 0.01
}

pub fn gex_batch(gamma:&[f64], oi:&[f64], multiplier:f64, spot:f64, dealer_sign:&[f64], out:&mut[f64])->Result<SimdBackend,String>{
    let n=gamma.len(); if oi.len()!=n||dealer_sign.len()!=n||out.len()<n{return Err("length mismatch".into())}
    // v1.24 keeps this portable reference path as the parity oracle. The AVX2/AVX-512
    // compiled kernels are promoted only after bit/tolerance parity tests on target CPU.
    for i in 0..n { out[i]=gex_scalar(gamma[i],oi[i],multiplier,spot,dealer_sign[i]); }
    Ok(detected_backend())
}
