// Lightweight, audio-driven expression cues. These are not phoneme-aligned visemes.
// Read the actual playback analyser so silence, stalls and interruption close the mouth.
export type MouthShape = {open:number; wide:number; round:number};
export function audioMotion(samples: Uint8Array, spectrum: Uint8Array, sampleRate:number) {
  let power=0;
  for(const sample of samples)power+=((sample-128)/128)**2;
  const rms=Math.sqrt(power/samples.length);
  if(rms<.009)return {level:0,mouth:{open:0,wide:0,round:0}};
  const binHz=sampleRate/(spectrum.length*2);
  const band=(from:number,to:number)=>{
    let sum=0,count=0;
    for(let i=Math.ceil(from/binHz);i<Math.min(spectrum.length,Math.ceil(to/binHz));i++){
      sum+=spectrum[i]/255;count++;
    }
    return count?sum/count:0;
  };
  const low=band(180,650),mid=band(650,1800),high=band(1800,4000);
  const total=low+mid+high+.001;
  const level=Math.min(1,Math.max(0,(rms-.009)*8));
  return {level,mouth:{
    open:Math.min(1,level*1.45),
    wide:Math.min(1,(mid+high)/total),
    round:Math.min(1,low/total),
  }};
}
