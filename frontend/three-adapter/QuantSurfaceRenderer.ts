/* Optional Three.js adapter source. The packaged terminal prefers raw WebGPU for fewer
 * runtime dependencies and falls back to raw WebGL/GLSL. This adapter provides the
 * requested Three.js integration when a project chooses to bundle Three.js. */
import * as THREE from 'three';
export class QuantSurfaceRenderer {
  private scene=new THREE.Scene(); private camera=new THREE.PerspectiveCamera(42,1,.05,100);
  private renderer:THREE.WebGLRenderer; private mesh?:THREE.Mesh<THREE.PlaneGeometry,THREE.ShaderMaterial>;
  constructor(private canvas:HTMLCanvasElement){this.renderer=new THREE.WebGLRenderer({canvas,antialias:true,alpha:false});this.camera.position.set(0,-3.1,2.3);}
  setMatrix(values:Float32Array,rows:number,cols:number){
    const tex=new THREE.DataTexture(values,cols,rows,THREE.RedFormat,THREE.FloatType);tex.needsUpdate=true;
    const geo=new THREE.PlaneGeometry(2.8,2.1,Math.max(1,cols-1),Math.max(1,rows-1));
    const mat=new THREE.ShaderMaterial({uniforms:{quantDataTexture:{value:tex},extrusionScale:{value:.75}},vertexShader:`uniform sampler2D quantDataTexture;uniform float extrusionScale;varying float vExposure;void main(){float q=texture2D(quantDataTexture,uv).r;vec3 p=position+normal*q*extrusionScale;vExposure=q;gl_Position=projectionMatrix*modelViewMatrix*vec4(p,1.0);}`,fragmentShader:`varying float vExposure;void main(){vec3 pos=vec3(.18,.62,.42),neg=vec3(.85,.27,.35),neu=vec3(.035,.055,.075);float a=clamp(abs(vExposure),0.,1.);vec3 c=mix(neu,vExposure>=0.?pos:neg,a);gl_FragColor=vec4(c,.96);}`,side:THREE.DoubleSide});
    if(this.mesh)this.scene.remove(this.mesh);this.mesh=new THREE.Mesh(geo,mat);this.scene.add(this.mesh);this.render();
  }
  render(){const r=this.canvas.getBoundingClientRect();this.renderer.setSize(r.width,r.height,false);this.camera.aspect=r.width/Math.max(1,r.height);this.camera.updateProjectionMatrix();this.renderer.render(this.scene,this.camera);}
}
