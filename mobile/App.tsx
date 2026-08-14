import React, { useState } from 'react';
import { Alert, Linking, SafeAreaView, ScrollView, StyleSheet, Text, TouchableOpacity, View } from 'react-native';
import * as ImagePicker from 'expo-image-picker';
import { StatusBar } from 'expo-status-bar';

const API = process.env.EXPO_PUBLIC_API_BASE_URL || 'http://127.0.0.1:8000';
type Ratio = '16:9'|'9:16'|'1:1';
type Layout = 'standard'|'gaming';
type Candidate = {start:number,end:number,score:number};

export default function App(){
  const [videoId,setVideoId]=useState<string|null>(null);
  const [name,setName]=useState('No video selected');
  const [ratio,setRatio]=useState<Ratio>('9:16');
  const [layout,setLayout]=useState<Layout>('standard');
  const [candidates,setCandidates]=useState<Candidate[]>([]);
  const [selected,setSelected]=useState<Candidate|null>(null);
  const [busy,setBusy]=useState(false);
  const [status,setStatus]=useState('');

  async function pickAndUpload(){
    const perm=await ImagePicker.requestMediaLibraryPermissionsAsync();
    if(!perm.granted){Alert.alert('Photos permission required');return}
    const result=await ImagePicker.launchImageLibraryAsync({mediaTypes:['videos'],quality:1});
    if(result.canceled)return;
    const asset=result.assets[0]; setName(asset.fileName||'Livestream'); setBusy(true); setStatus('Uploading…');
    const fd=new FormData();
    fd.append('video',{uri:asset.uri,name:asset.fileName||'livestream.mp4',type:asset.mimeType||'video/mp4'} as any);
    try{const r=await fetch(`${API}/api/upload`,{method:'POST',body:fd});const d=await r.json();if(!r.ok)throw new Error(d.detail||'Upload failed');setVideoId(d.video_id);setStatus(`Uploaded • ${Math.round(d.duration)} sec`);}catch(e:any){setStatus(e.message)}finally{setBusy(false)}
  }

  async function analyze(){
    if(!videoId)return;setBusy(true);setStatus('Finding exciting moments…');
    try{const r=await fetch(`${API}/api/videos/${videoId}/analyze`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({target_duration:30,limit:5})});const d=await r.json();if(!r.ok)throw new Error(d.detail||'Analysis failed');setCandidates(d.candidates);setSelected(d.candidates[0]||null);setStatus('Candidate moments ready.');}catch(e:any){setStatus(e.message)}finally{setBusy(false)}
  }

  async function exportClip(){
    if(!videoId||!selected)return;setBusy(true);setStatus('Rendering clip…');
    try{const r=await fetch(`${API}/api/videos/${videoId}/export`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({start:selected.start,end:selected.end,aspect:ratio,layout,face_corner:'top-right',face_width_fraction:0.30,face_height_fraction:0.34})});const d=await r.json();if(!r.ok)throw new Error(d.detail||'Export failed');setStatus('Export complete. Opening video…');await Linking.openURL(`${API}${d.download_url}`);}catch(e:any){setStatus(e.message)}finally{setBusy(false)}
  }

  function chooseRatio(r:Ratio){setRatio(r);if(r!=='9:16'&&layout==='gaming')setLayout('standard')}

  return <SafeAreaView style={styles.safe}><StatusBar style="light"/><ScrollView contentContainerStyle={styles.container}>
    <Text style={styles.logo}>Clip<Text style={styles.accent}>Pilot</Text></Text>
    <Text style={styles.subtitle}>Livestream → social clips</Text>
    <View style={styles.card}><Text style={styles.h2}>1. Livestream</Text><Text style={styles.muted}>{name}</Text><Button title={busy?'Working…':'Choose video'} onPress={pickAndUpload} disabled={busy}/>{videoId&&<Button title="Auto-find clips" onPress={analyze} disabled={busy}/>}</View>
    {candidates.length>0&&<View style={styles.card}><Text style={styles.h2}>2. Candidate moments</Text>{candidates.map((c,i)=><TouchableOpacity key={i} style={[styles.candidate,selected===c&&styles.selected]} onPress={()=>setSelected(c)}><View><Text style={styles.bold}>Candidate {i+1}</Text><Text style={styles.muted}>{Math.round(c.start)}s → {Math.round(c.end)}s</Text></View><Text style={styles.score}>{c.score}</Text></TouchableOpacity>)}</View>}
    <View style={styles.card}><Text style={styles.h2}>3. Format</Text><Text style={styles.label}>Aspect ratio</Text><View style={styles.row}>{(['16:9','9:16','1:1'] as Ratio[]).map(r=><Chip key={r} label={r} active={ratio===r} onPress={()=>chooseRatio(r)}/>)}</View><Text style={styles.label}>Layout</Text><View style={styles.row}><Chip label="Standard" active={layout==='standard'} onPress={()=>setLayout('standard')}/><Chip label="Face-cam top" active={layout==='gaming'} onPress={()=>{if(ratio!=='9:16')Alert.alert('Choose 9:16 first');else setLayout('gaming')}}/></View><Button title="Export clip" onPress={exportClip} disabled={busy||!selected}/></View>
    {!!status&&<Text style={styles.status}>{status}</Text>}
  </ScrollView></SafeAreaView>
}

function Button({title,onPress,disabled=false}:{title:string,onPress:()=>void,disabled?:boolean}){return <TouchableOpacity disabled={disabled} onPress={onPress} style={[styles.button,disabled&&{opacity:.45}]}><Text style={styles.buttonText}>{title}</Text></TouchableOpacity>}
function Chip({label,active,onPress}:{label:string,active:boolean,onPress:()=>void}){return <TouchableOpacity onPress={onPress} style={[styles.chip,active&&styles.chipActive]}><Text style={styles.bold}>{label}</Text></TouchableOpacity>}

const styles=StyleSheet.create({safe:{flex:1,backgroundColor:'#0b0d12'},container:{padding:22,paddingBottom:70},logo:{fontSize:36,fontWeight:'900',color:'#fff',letterSpacing:-1.2},accent:{color:'#8ea0ff'},subtitle:{color:'#929db2',marginBottom:22},card:{backgroundColor:'#151923',borderColor:'#262d3c',borderWidth:1,borderRadius:18,padding:18,marginBottom:15},h2:{fontSize:20,fontWeight:'800',color:'#fff',marginBottom:10},muted:{color:'#919bad'},bold:{fontWeight:'800',color:'#fff'},label:{color:'#aab3c6',marginTop:10,marginBottom:8},button:{backgroundColor:'#6d7cff',borderRadius:12,padding:13,alignItems:'center',marginTop:12},buttonText:{fontWeight:'900',color:'#fff'},row:{flexDirection:'row',gap:8,flexWrap:'wrap'},chip:{borderWidth:1,borderColor:'#323a4e',paddingVertical:9,paddingHorizontal:13,borderRadius:999,marginBottom:4},chipActive:{backgroundColor:'#6d7cff',borderColor:'#6d7cff'},candidate:{paddingVertical:13,borderBottomColor:'#293041',borderBottomWidth:1,flexDirection:'row',justifyContent:'space-between'},selected:{backgroundColor:'#20263a'},score:{color:'#aeb8ff',fontSize:18,fontWeight:'900'},status:{color:'#cbd2df',textAlign:'center',marginTop:2}})
