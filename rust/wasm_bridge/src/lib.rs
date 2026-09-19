use wasm_bindgen::prelude::*;

#[wasm_bindgen]
pub struct QuantBridge { buffer: Vec<f32> }

#[wasm_bindgen]
impl QuantBridge {
    #[wasm_bindgen(constructor)]
    pub fn new(size:usize)->QuantBridge{QuantBridge{buffer:vec![0.0;size]}}
    pub fn capacity(&self)->usize{self.buffer.len()}
    pub fn pointer(&self)->usize{self.buffer.as_ptr() as usize}
    pub fn process_surface_frame(&mut self, raw:&[u8])->Result<usize,JsValue>{
        // ITMS header = 26 bytes, followed by UTF-8 field name then little-endian f32.
        if raw.len()<26 || &raw[0..4]!=b"ITMS" || raw[4]!=1{return Err(JsValue::from_str("invalid ITMS frame"));}
        let name_len=raw[5] as usize;
        let rows=u16::from_le_bytes([raw[6],raw[7]]) as usize;
        let cols=u16::from_le_bytes([raw[8],raw[9]]) as usize;
        let count=rows.checked_mul(cols).ok_or_else(||JsValue::from_str("matrix overflow"))?;
        let off=26usize.checked_add(name_len).ok_or_else(||JsValue::from_str("offset overflow"))?;
        let need=off.checked_add(count*4).ok_or_else(||JsValue::from_str("payload overflow"))?;
        if raw.len()!=need{return Err(JsValue::from_str("truncated/oversized ITMS frame"));}
        if self.buffer.len()<count{self.buffer.resize(count,0.0);}
        for i in 0..count{let j=off+i*4;self.buffer[i]=f32::from_le_bytes([raw[j],raw[j+1],raw[j+2],raw[j+3]]);}
        Ok(count)
    }
}

#[wasm_bindgen]
pub fn wasm_memory() -> JsValue { wasm_bindgen::memory() }
