import React from 'react';
import {Img,interpolate,staticFile,useCurrentFrame,useVideoConfig} from 'remotion';
type Props={photo:string;secondPhoto:string;layout:number;label:string;archive:string;seconds:number};
export const PhotoPlate=({photo,secondPhoto,layout,label,archive,seconds}:Props)=>{
 const f=useCurrentFrame();const {fps}=useVideoConfig();
 const progress=Math.min(1,f/Math.max(1,seconds*fps-1));
 const zoom=1+progress*.055;const slide=(progress-.5)*28;
 const reveal=interpolate(f,[0,12],[0,1],{extrapolateRight:'clamp'});
 const source=staticFile(photo),other=staticFile(secondPhoto||photo);
 const base:React.CSSProperties={position:'absolute',objectFit:'contain'};
 return <div style={{position:'absolute',inset:0,overflow:'hidden',background:layout%3===1?'#eee9df':'#0d1520'}}>
  <Img src={source} style={{position:'absolute',inset:-100,width:2120,height:1280,objectFit:'cover',filter:'blur(38px) brightness(.32)',transform:`scale(${1.08-progress*.025}) translateX(${-slide}px)`}}/>
  {layout%3===0&&<Img src={source} style={{...base,left:60,top:30,width:1800,height:1020,transform:`translateX(${slide}px) scale(${zoom})`,opacity:.65+.35*reveal}}/>}
  {layout%3===1&&<>
   <div style={{position:'absolute',inset:0,background:'#eee9df'}}/>
   <Img src={source} style={{...base,left:45,top:55,width:1080,height:970,transform:`translateX(${slide*.5}px) scale(${zoom})`}}/>
   <div style={{position:'absolute',left:1220,top:300,width:585,fontFamily:'Arial',opacity:reveal}}>
    <div style={{fontSize:23,letterSpacing:4,color:'#72614a',marginBottom:45}}>THE STORY</div>
    <div style={{fontSize:76,fontWeight:700,letterSpacing:-2,lineHeight:1.15,color:'#0d1520'}}>{label}</div>
    <div style={{height:3,width:95,background:'#ae8755',marginTop:45}}/>
   </div>
  </>}
  {layout%3===2&&<>
   <div style={{position:'absolute',left:955,top:50,bottom:50,width:2,background:'#d5b383',opacity:.5}}/>
   <Img src={source} style={{...base,left:55,top:45,width:850,height:990,transform:`translateY(${-slide}px) scale(${zoom})`}}/>
   <Img src={other} style={{...base,left:1015,top:45,width:850,height:990,transform:`translateY(${slide}px) scale(${1.055-progress*.025})`}}/>
  </>}
  <div style={{position:'absolute',left:60,top:40,padding:'9px 13px',background:'#0d1520b8',color:'#eee9df',font:'19px Arial',letterSpacing:3,opacity:f<fps*2.2?1:Math.max(0,1-(f-fps*2.2)/12)}}>{archive||'ARCHIVE'}</div>
 </div>;
};
