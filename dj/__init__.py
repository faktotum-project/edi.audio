"""Pipeline che trasforma un file audio in una traccia pronta per il mixaggio.

Il pacchetto è organizzato per stadi, nell'ordine in cui girano:

  sources  → trova il candidato audio migliore per un brano
  quality  → misura la qualità reale di un candidato (non quella dichiarata)
  analysis → BPM, beat, chiave, struttura (via worker in un venv separato)
  cues     → traduce la struttura in hot cue e loop agganciati alla battuta
  serato   → serializza beatgrid e cue nel formato binario di Serato
  tags     → scrive tutto nel file FLAC
  naming   → nomi file ordinabili per BPM e notazione Camelot
  export   → playlist M3U8 e Rekordbox XML
  pipeline → orchestra il tutto
"""
