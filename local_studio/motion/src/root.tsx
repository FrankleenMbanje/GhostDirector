import React from 'react';
import {Composition, Easing, Interactive, interpolate, useCurrentFrame, useVideoConfig} from 'remotion';
import {MapPlate,MapPoint} from './MapPlate';
import {PhotoPlate} from './PhotoPlate';

type Point={label:string;detail:string};
type Props={headline:string; detail:string; eyebrow:string; variant:number; kind:string; points:Point[];place?:MapPoint};
const ink='#0d1520', paper='#eee9df', gold='#d5b383';
const ease={extrapolateLeft:'clamp' as const,extrapolateRight:'clamp' as const,easing:Easing.bezier(.16,1,.3,1)};

const GraphicInner=({headline,detail,eyebrow,variant,kind,points,place}:Props)=>{
 const f=useCurrentFrame();const {fps}=useVideoConfig();
 if(kind==='map'&&place)return <MapPlate place={place}/>;
 const light=variant%3===1;
 const timeline=kind==='timeline'&&points.length>=2;
 const comparison=kind==='comparison'&&points.length>=2;
 return <>
  <div style={{position:'absolute',inset:0,background:light?paper:ink}}/>
  <svg width="1920" height="1080" style={{position:'absolute',inset:0,opacity:.25}}>
   <defs><pattern id="grid" width="90" height="90" patternUnits="userSpaceOnUse"><path d="M90 0H0V90" fill="none" stroke={light?'#b9b7b0':'#46586a'} strokeWidth="1"/></pattern></defs>
   <rect width="1920" height="1080" fill="url(#grid)"/>
   <circle cx="1730" cy="920" r={interpolate(f,[0,6*fps],[270,320],ease)} fill="none" stroke={gold} strokeWidth="2"/>
   <circle cx="1730" cy="920" r="230" fill="none" stroke={gold} strokeWidth="1"/>
  </svg>
  <div style={{position:'absolute',left:115,top:110,color:light?'#566471':'#8f9dab',fontFamily:'Arial',fontSize:25,letterSpacing:5}}>{eyebrow}</div>
  <div style={{position:'absolute',left:110,top:comparison?195:250,width:timeline?850:1560,overflow:'hidden'}}>
   <div style={{fontFamily:'Arial',fontSize:comparison?88:timeline?104:headline.length>35?105:136,fontWeight:700,letterSpacing:-4,lineHeight:1.08,color:light?ink:paper,translate:interpolate(f,[0,.7*fps],['0px 180px','0px 0px'],ease),opacity:interpolate(f,[0,.45*fps],[0,1],ease)}}>{headline}</div>
  </div>
  {!comparison&&<div style={{position:'absolute',left:115,top:690,height:3,width:interpolate(f,[.3*fps,1.1*fps],[0,150],ease),background:light?'#9a7245':gold}}/>}
  <div style={{position:'absolute',left:115,top:comparison?405:750,width:timeline?850:1440,color:light?'#50606b':gold,fontFamily:'Arial',fontSize:timeline?36:comparison?34:44,lineHeight:1.35,opacity:interpolate(f,[.55*fps,1.2*fps],[0,1],ease),translate:interpolate(f,[.55*fps,1.2*fps],['0px 24px','0px 0px'],ease)}}>{detail}</div>
  {timeline&&<>
   <div style={{position:'absolute',left:1135,top:300,width:3,height:interpolate(f,[.4*fps,1.9*fps],[0,340],ease),background:gold}}/>
   {points.slice(0,3).map((p,i)=><div key={i} style={{position:'absolute',left:1118,top:280+i*170,opacity:interpolate(f,[(.3+i*.45)*fps,(.75+i*.45)*fps],[0,1],ease)}}><div style={{position:'absolute',left:5,top:22,width:28,height:28,borderRadius:'50%',background:gold}}/><div style={{marginLeft:90,width:540,fontFamily:'Arial',fontSize:49,fontWeight:700,color:light?ink:paper}}>{p.label}</div><div style={{marginLeft:90,marginTop:18,width:540,fontFamily:'Arial',fontSize:29,lineHeight:1.35,color:light?'#50606b':'#98a8b6'}}>{p.detail}</div></div>)}
  </>}
  {comparison&&points.slice(0,2).map((p,i)=><div key={i} style={{position:'absolute',left:115+i*870,top:550,width:790,height:250,padding:35,boxSizing:'border-box',borderTop:`3px solid ${gold}`,background:light?'#dedbd3':'#1c2b39',opacity:interpolate(f,[(.5+i*.25)*fps,(1+i*.25)*fps],[0,1],ease),translate:interpolate(f,[(.5+i*.25)*fps,(1+i*.25)*fps],['0px 40px','0px 0px'],ease)}}><div style={{fontFamily:'Arial',fontSize:65,fontWeight:700,color:light?ink:paper}}>{p.label}</div><div style={{fontFamily:'Arial',fontSize:33,lineHeight:1.35,marginTop:25,color:light?'#50606b':gold}}>{p.detail}</div></div>)}
 </>;
};
const GraphicPlate=Interactive.create({Component:GraphicInner,layout:'absolute-fill'});
export const Root=()=> <>
 <Composition id="GraphicPlate" component={GraphicPlate} durationInFrames={210} fps={30} width={1920} height={1080} defaultProps={{headline:'The facts come first',detail:'Original local motion design. No paid image generation.',eyebrow:'THE FAME FILES',variant:0,kind:'statement',points:[]}}/>
 <Composition id="PhotoPlate" component={PhotoPlate} durationInFrames={300} calculateMetadata={({props})=>({durationInFrames:Math.max(1,Math.round(props.seconds*30))})} fps={30} width={1920} height={1080} defaultProps={{photo:'',secondPhoto:'',layout:0,label:'',archive:'ARCHIVE',seconds:10}}/>
</>;
