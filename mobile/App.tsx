import React, { useState } from 'react';
import {
  ActivityIndicator,
  Alert,
  Linking,
  SafeAreaView,
  ScrollView,
  Switch,
  StyleSheet,
  Text,
  TouchableOpacity,
  View,
} from 'react-native';
import * as ImagePicker from 'expo-image-picker';
import { StatusBar } from 'expo-status-bar';

const API = process.env.EXPO_PUBLIC_API_BASE_URL || 'http://127.0.0.1:8000';

type Ratio = '16:9' | '9:16' | '1:1';
type Layout = 'standard' | 'gaming';
type CaptionScheme = 'pilot-lime' | 'ocean' | 'sunset' | 'neon-pink' | 'violet';
type Candidate = { start: number; end: number; score: number };

const CAPTION_SCHEMES: { value: CaptionScheme; label: string; colour: string }[] = [
  { value: 'pilot-lime', label: 'Lime', colour: '#b9f34a' },
  { value: 'ocean', label: 'Ocean', colour: '#35dcff' },
  { value: 'sunset', label: 'Gold', colour: '#ffd24a' },
  { value: 'neon-pink', label: 'Pink', colour: '#ff4fd8' },
  { value: 'violet', label: 'Violet', colour: '#a98bff' },
];

export default function App() {
  const [videoId, setVideoId] = useState<string | null>(null);
  const [name, setName] = useState('No source loaded');
  const [ratio, setRatio] = useState<Ratio>('9:16');
  const [layout, setLayout] = useState<Layout>('standard');
  const [liveCaptions, setLiveCaptions] = useState(false);
  const [captionScheme, setCaptionScheme] = useState<CaptionScheme>('pilot-lime');
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [selected, setSelected] = useState<Candidate | null>(null);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState('Preflight ready');

  async function pickAndUpload() {
    const permission = await ImagePicker.requestMediaLibraryPermissionsAsync();
    if (!permission.granted) {
      Alert.alert('Photos permission required');
      return;
    }

    const result = await ImagePicker.launchImageLibraryAsync({ mediaTypes: ['videos'], quality: 1 });
    if (result.canceled) return;

    const asset = result.assets[0];
    setName(asset.fileName || 'Livestream');
    setBusy(true);
    setStatus('Uploading source video…');

    const data = new FormData();
    data.append('video', {
      uri: asset.uri,
      name: asset.fileName || 'livestream.mp4',
      type: asset.mimeType || 'video/mp4',
    } as any);

    try {
      const response = await fetch(`${API}/api/upload`, { method: 'POST', body: data });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || 'Upload failed');
      setVideoId(payload.video_id);
      setStatus(`Source ready · ${Math.round(payload.duration)} sec`);
    } catch (error: any) {
      setStatus(error.message);
    } finally {
      setBusy(false);
    }
  }

  async function analyze() {
    if (!videoId) return;
    setBusy(true);
    setStatus('Moment Radar is scanning…');

    try {
      const response = await fetch(`${API}/api/videos/${videoId}/analyze`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target_duration: 30, limit: 5 }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || 'Analysis failed');
      setCandidates(payload.candidates);
      setSelected(payload.candidates[0] || null);
      setStatus('Strongest moments are ready');
    } catch (error: any) {
      setStatus(error.message);
    } finally {
      setBusy(false);
    }
  }

  async function exportClip() {
    if (!videoId || !selected) return;
    setBusy(true);
    setStatus('Clearing clip for launch…');

    try {
      const response = await fetch(`${API}/api/videos/${videoId}/export`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          start: selected.start,
          end: selected.end,
          aspect: ratio,
          layout,
          face_corner: 'top-right',
          face_width_fraction: 0.3,
          face_height_fraction: 0.34,
          live_captions: liveCaptions,
          live_caption_scheme: captionScheme,
        }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || 'Export failed');
      setStatus('Clip launched · opening video');
      await Linking.openURL(`${API}${payload.download_url}`);
    } catch (error: any) {
      setStatus(error.message);
    } finally {
      setBusy(false);
    }
  }

  function chooseRatio(nextRatio: Ratio) {
    setRatio(nextRatio);
    if (nextRatio !== '9:16' && layout === 'gaming') setLayout('standard');
  }

  return (
    <SafeAreaView style={styles.safe}>
      <StatusBar style="light" />
      <ScrollView contentContainerStyle={styles.container}>
        <View style={styles.header}>
          <View style={styles.brandRow}>
            <View style={styles.brandMark}><Text style={styles.brandGlyph}>✈︎</Text></View>
            <View>
              <Text style={styles.logo}>Clip Farm Pilot</Text>
              <Text style={styles.flightDeck}>CREATOR FLIGHT DECK</Text>
            </View>
          </View>
          <View style={styles.online}><View style={styles.onlineDot} /><Text style={styles.onlineText}>ONLINE</Text></View>
        </View>

        <View style={styles.route}>
          <RouteStep number="01" label="Source" active />
          <RouteStep number="02" label="Radar" />
          <RouteStep number="03" label="Frame" />
          <RouteStep number="04" label="Launch" />
        </View>

        <Panel code="01" title="Flight source">
          <Text style={styles.sourceName} numberOfLines={1}>{name}</Text>
          <Button title={videoId ? 'Replace source video' : 'Load source video'} onPress={pickAndUpload} disabled={busy} />
          {videoId && <Button title="Scan with Moment Radar" variant="secondary" onPress={analyze} disabled={busy} />}
        </Panel>

        {candidates.length > 0 && (
          <Panel code="02" title="Moment radar">
            <Text style={styles.panelHint}>Select a high-energy moment for export.</Text>
            {candidates.map((candidate, index) => (
              <TouchableOpacity
                key={`${candidate.start}-${index}`}
                style={[styles.candidate, selected === candidate && styles.candidateSelected]}
                onPress={() => setSelected(candidate)}
              >
                <View>
                  <Text style={styles.candidateTitle}>MOMENT {String(index + 1).padStart(2, '0')}</Text>
                  <Text style={styles.candidateTime}>{Math.round(candidate.start)}s → {Math.round(candidate.end)}s</Text>
                </View>
                <View style={styles.scoreBadge}><Text style={styles.score}>{candidate.score}</Text></View>
              </TouchableOpacity>
            ))}
          </Panel>
        )}

        <Panel code="03" title="Output route">
          <Text style={styles.label}>FORMAT</Text>
          <View style={styles.row}>
            {(['16:9', '9:16', '1:1'] as Ratio[]).map((item) => (
              <Chip key={item} label={item} active={ratio === item} onPress={() => chooseRatio(item)} />
            ))}
          </View>

          <Text style={styles.label}>LAYOUT</Text>
          <View style={styles.row}>
            <Chip label="Standard" active={layout === 'standard'} onPress={() => setLayout('standard')} />
            <Chip
              label="Cockpit"
              active={layout === 'gaming'}
              onPress={() => {
                if (ratio !== '9:16') Alert.alert('Cockpit layout uses the 9:16 format');
                else setLayout('gaming');
              }}
            />
          </View>
        </Panel>

        <Panel code="CC" title="Live captions">
          <View style={styles.switchRow}>
            <View style={styles.switchCopy}>
              <Text style={styles.switchTitle}>Word-by-word highlights</Text>
              <Text style={styles.panelHint}>Transcribe English speech and highlight the word being spoken.</Text>
            </View>
            <Switch
              value={liveCaptions}
              onValueChange={setLiveCaptions}
              trackColor={{ false: '#263c49', true: '#357894' }}
              thumbColor={liveCaptions ? '#5bd6ff' : '#8aa0ae'}
            />
          </View>
          <View style={[styles.captionSchemes, !liveCaptions && styles.captionSchemesDisabled]}>
            {CAPTION_SCHEMES.map((scheme) => (
              <TouchableOpacity
                key={scheme.value}
                disabled={!liveCaptions}
                onPress={() => setCaptionScheme(scheme.value)}
                style={[styles.captionScheme, captionScheme === scheme.value && styles.captionSchemeActive]}
              >
                <View style={[styles.captionColour, { backgroundColor: scheme.colour }]} />
                <Text style={styles.captionSchemeText}>{scheme.label}</Text>
              </TouchableOpacity>
            ))}
          </View>
        </Panel>

        <View style={styles.statusPanel}>
          <View style={[styles.statusLamp, busy && styles.statusLampBusy]} />
          <View style={styles.statusCopy}>
            <Text style={styles.statusLabel}>PREFLIGHT STATUS</Text>
            <Text style={styles.statusText}>{status}</Text>
          </View>
          {busy && <ActivityIndicator color="#5bd6ff" />}
        </View>

        <Button title="Launch clip" onPress={exportClip} disabled={busy || !selected} />
      </ScrollView>
    </SafeAreaView>
  );
}

function Panel({ code, title, children }: { code: string; title: string; children: React.ReactNode }) {
  return (
    <View style={styles.panel}>
      <View style={styles.panelAccent} />
      <View style={styles.panelHead}>
        <Text style={styles.panelCode}>{code}</Text>
        <Text style={styles.panelTitle}>{title}</Text>
        <View style={styles.panelLine} />
      </View>
      {children}
    </View>
  );
}

function RouteStep({ number, label, active = false }: { number: string; label: string; active?: boolean }) {
  return (
    <View style={[styles.routeStep, active && styles.routeStepActive]}>
      <Text style={styles.routeNumber}>{number}</Text>
      <Text style={[styles.routeLabel, active && styles.routeLabelActive]}>{label}</Text>
    </View>
  );
}

function Button({
  title,
  onPress,
  disabled = false,
  variant = 'primary',
}: {
  title: string;
  onPress: () => void;
  disabled?: boolean;
  variant?: 'primary' | 'secondary';
}) {
  return (
    <TouchableOpacity
      disabled={disabled}
      onPress={onPress}
      style={[styles.button, variant === 'secondary' && styles.buttonSecondary, disabled && styles.disabled]}
    >
      <Text style={[styles.buttonText, variant === 'secondary' && styles.buttonTextSecondary]}>{title}</Text>
    </TouchableOpacity>
  );
}

function Chip({ label, active, onPress }: { label: string; active: boolean; onPress: () => void }) {
  return (
    <TouchableOpacity onPress={onPress} style={[styles.chip, active && styles.chipActive]}>
      <View style={[styles.chipLamp, active && styles.chipLampActive]} />
      <Text style={[styles.chipText, active && styles.chipTextActive]}>{label}</Text>
    </TouchableOpacity>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: '#06101a' },
  container: { padding: 20, paddingBottom: 70 },
  header: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', marginBottom: 20 },
  brandRow: { flexDirection: 'row', alignItems: 'center', gap: 12 },
  brandMark: {
    width: 44,
    height: 44,
    alignItems: 'center',
    justifyContent: 'center',
    borderRadius: 22,
    borderWidth: 1,
    borderColor: '#387f9c',
    backgroundColor: '#102b3d',
  },
  brandGlyph: { color: '#5bd6ff', fontSize: 23, fontWeight: '900' },
  logo: { color: '#f2f8fc', fontSize: 22, fontWeight: '900', letterSpacing: -0.6 },
  flightDeck: { marginTop: 3, color: '#7093a8', fontSize: 9, fontWeight: '800', letterSpacing: 1.35 },
  online: { flexDirection: 'row', alignItems: 'center', gap: 6 },
  onlineDot: { width: 6, height: 6, borderRadius: 3, backgroundColor: '#6ce6ab' },
  onlineText: { color: '#7f9aad', fontSize: 8, fontWeight: '800', letterSpacing: 1 },
  route: { flexDirection: 'row', gap: 6, marginBottom: 14 },
  routeStep: {
    flex: 1,
    minHeight: 44,
    justifyContent: 'center',
    paddingHorizontal: 9,
    borderWidth: 1,
    borderColor: '#173448',
    borderRadius: 9,
    backgroundColor: '#091722',
  },
  routeStepActive: { borderColor: '#397b96', backgroundColor: '#0f293a' },
  routeNumber: { color: '#5bd6ff', fontSize: 8, fontWeight: '900', letterSpacing: 0.8 },
  routeLabel: { marginTop: 3, color: '#668297', fontSize: 9, fontWeight: '800' },
  routeLabelActive: { color: '#dff7ff' },
  panel: {
    position: 'relative',
    overflow: 'hidden',
    padding: 17,
    marginBottom: 13,
    borderWidth: 1,
    borderColor: '#19384d',
    borderRadius: 16,
    backgroundColor: '#0b1824',
  },
  panelAccent: { position: 'absolute', top: 0, left: 20, width: 52, height: 2, backgroundColor: '#5bd6ff' },
  panelHead: { flexDirection: 'row', alignItems: 'center', gap: 8, marginBottom: 13 },
  panelCode: {
    paddingHorizontal: 6,
    paddingVertical: 3,
    overflow: 'hidden',
    borderWidth: 1,
    borderColor: '#285b73',
    borderRadius: 5,
    color: '#5bd6ff',
    fontSize: 8,
    fontWeight: '900',
  },
  panelTitle: { color: '#f2f8fc', fontSize: 15, fontWeight: '800' },
  panelLine: { flex: 1, height: 1, backgroundColor: '#18384d' },
  panelHint: { marginBottom: 8, color: '#91a9ba', fontSize: 11 },
  switchRow: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', gap: 12 },
  switchCopy: { flex: 1 },
  switchTitle: { marginBottom: 4, color: '#f2f8fc', fontSize: 13, fontWeight: '800' },
  captionSchemes: { flexDirection: 'row', flexWrap: 'wrap', gap: 7, marginTop: 11 },
  captionSchemesDisabled: { opacity: 0.42 },
  captionScheme: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 6,
    minHeight: 34,
    paddingHorizontal: 9,
    borderWidth: 1,
    borderColor: '#214258',
    borderRadius: 9,
    backgroundColor: '#081621',
  },
  captionSchemeActive: { borderColor: '#4f8da8', backgroundColor: '#102c3f' },
  captionColour: { width: 8, height: 8, borderRadius: 4 },
  captionSchemeText: { color: '#dcebf3', fontSize: 10, fontWeight: '800' },
  sourceName: { color: '#91a9ba', marginBottom: 2 },
  button: {
    minHeight: 48,
    alignItems: 'center',
    justifyContent: 'center',
    marginTop: 11,
    borderRadius: 11,
    backgroundColor: '#ffbd59',
  },
  buttonSecondary: { borderWidth: 1, borderColor: '#28526a', backgroundColor: '#122b3d' },
  buttonText: { color: '#241500', fontSize: 13, fontWeight: '900', letterSpacing: 0.15 },
  buttonTextSecondary: { color: '#e7f8ff' },
  disabled: { opacity: 0.4 },
  label: { marginTop: 10, marginBottom: 8, color: '#80cce8', fontSize: 9, fontWeight: '800', letterSpacing: 1.1 },
  row: { flexDirection: 'row', flexWrap: 'wrap', gap: 8 },
  chip: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 7,
    minHeight: 42,
    paddingHorizontal: 14,
    borderWidth: 1,
    borderColor: '#214258',
    borderRadius: 10,
    backgroundColor: '#081621',
  },
  chipActive: { borderColor: '#3985a4', backgroundColor: '#102c3f' },
  chipLamp: { width: 6, height: 6, borderRadius: 3, backgroundColor: '#405b6b' },
  chipLampActive: { backgroundColor: '#5bd6ff' },
  chipText: { color: '#91a9ba', fontSize: 12, fontWeight: '800' },
  chipTextActive: { color: '#e4f9ff' },
  candidate: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    minHeight: 62,
    paddingHorizontal: 12,
    marginTop: 7,
    borderWidth: 1,
    borderColor: '#19384d',
    borderRadius: 10,
    backgroundColor: '#07141f',
  },
  candidateSelected: { borderColor: '#3985a4', backgroundColor: '#102a3b' },
  candidateTitle: { color: '#80cce8', fontSize: 8, fontWeight: '900', letterSpacing: 1 },
  candidateTime: { marginTop: 4, color: '#f2f8fc', fontSize: 12, fontWeight: '700' },
  scoreBadge: { minWidth: 40, paddingVertical: 6, borderRadius: 7, backgroundColor: '#12364a' },
  score: { color: '#5bd6ff', textAlign: 'center', fontSize: 15, fontWeight: '900' },
  statusPanel: {
    flexDirection: 'row',
    alignItems: 'center',
    minHeight: 68,
    paddingHorizontal: 15,
    borderWidth: 1,
    borderColor: '#253a45',
    borderRadius: 14,
    backgroundColor: '#0a1721',
  },
  statusLamp: { width: 9, height: 9, borderRadius: 5, backgroundColor: '#6ce6ab' },
  statusLampBusy: { backgroundColor: '#5bd6ff' },
  statusCopy: { flex: 1, marginLeft: 11 },
  statusLabel: { color: '#668297', fontSize: 8, fontWeight: '900', letterSpacing: 1.1 },
  statusText: { marginTop: 4, color: '#dcebf3', fontSize: 12, fontWeight: '700' },
});
