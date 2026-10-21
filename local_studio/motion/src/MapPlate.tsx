import React,{useEffect,useRef,useState} from 'react';
import {Easing,interpolate,staticFile,useCurrentFrame,useDelayRender,useVideoConfig} from 'remotion';
import {Map as LibreMap,setWorkerUrl} from 'maplibre-gl';
import type {FeatureCollection} from 'geojson';
import {featureCollection,point} from '@turf/turf';
import geography from './geography.json';
import 'maplibre-gl/dist/maplibre-gl.css';

export type MapPoint={name:string;longitude:number;latitude:number;country:string};
export const MapPlate=({place}:{place:MapPoint})=>{
 const container=useRef<HTMLDivElement>(null);
 const [map,setMap]=useState<LibreMap|null>(null);
 const {delayRender,continueRender,cancelRender}=useDelayRender();
 const [loading]=useState(()=>delayRender('Loading local vector map'));
 const frame=useCurrentFrame();const {fps}=useVideoConfig();
 useEffect(()=>{
  if(!container.current)return;
  setWorkerUrl(staticFile('maplibre/maplibre-gl-worker.mjs'));
  const instance=new LibreMap({container:container.current,center:[place.longitude,place.latitude],zoom:place.country==='US'?4:3,interactive:false,attributionControl:false,fadeDuration:0,canvasContextAttributes:{preserveDrawingBuffer:true},style:{version:8,sources:{boundaries:{type:'geojson',data:geography as unknown as FeatureCollection},location:{type:'geojson',data:featureCollection([point([place.longitude,place.latitude])])}},layers:[
   {id:'water',type:'background',paint:{'background-color':'#0d1520'}},
   {id:'land',type:'fill',source:'boundaries',filter:['==',['get','level'],'country'],paint:{'fill-color':['case',['==',['get','code'],place.country],'#30414b','#172631'],'fill-outline-color':'#53616a'}},
   {id:'states',type:'line',source:'boundaries',filter:['==',['get','level'],'state'],paint:{'line-color':'#9b977c','line-width':1,'line-opacity':.55}},
   {id:'pulse',type:'circle',source:'location',paint:{'circle-radius':30,'circle-color':'#d5b383','circle-opacity':.12}},
   {id:'pin',type:'circle',source:'location',paint:{'circle-radius':9,'circle-color':'#d5b383','circle-stroke-color':'#eee9df','circle-stroke-width':3}},
  ]}});
  instance.on('error',event=>cancelRender(event.error));
  instance.on('load',()=>{
   instance.jumpTo({center:[place.longitude,place.latitude],zoom:place.country==='US'?4:3});
   instance.once('idle',()=>{setMap(instance);continueRender(loading);});
  });
 },[loading]);
 useEffect(()=>{
  if(!map)return;
  const handle=delayRender('Rendering locator pulse');
  map.once('idle',()=>continueRender(handle));
  const progress=(frame%(fps*2))/(fps*2);
  map.setPaintProperty('pulse','circle-radius',12+progress*37);
  map.setPaintProperty('pulse','circle-opacity',.38*(1-progress));
 },[map,frame]);
 const reveal=interpolate(frame,[8,35],[0,1],{extrapolateLeft:'clamp',extrapolateRight:'clamp',easing:Easing.bezier(.16,1,.3,1)});
 return <div style={{position:'absolute',inset:0,background:'#0d1520'}}>
  <div ref={container} style={{position:'absolute',left:0,top:0,width:1280,height:1080}}/>
  <div style={{position:'absolute',left:1070,top:0,width:260,height:1080,background:'linear-gradient(90deg,transparent,#0d1520)'}}/>
  <div style={{position:'absolute',left:1300,top:250,width:535,opacity:reveal,transform:`translateY(${(1-reveal)*30}px)`,fontFamily:'Arial'}}>
   <div style={{color:'#d5b383',fontSize:23,letterSpacing:5,marginBottom:50}}>LOCATION</div>
   <div style={{fontSize:place.name.length>22?76:96,fontWeight:700,lineHeight:1.1,letterSpacing:-3,color:'#eee9df'}}>{place.name}</div>
   <div style={{width:100,height:3,background:'#d5b383',margin:'45px 0'}}/>
   <div style={{fontSize:27,lineHeight:1.5,color:'#97a7b4'}}>Area locator</div>
  </div>
  <div style={{position:'absolute',left:60,bottom:24,color:'#9ba7ae',font:'16px Arial'}}>Natural Earth · Geocoding © OpenStreetMap contributors</div>
 </div>;
};
