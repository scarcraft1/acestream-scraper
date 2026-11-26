import { useUiText } from '../i18n/useUiText';
import { Alert, Button, LinearProgress, Stack, TextField } from '@mui/material';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { configService, type StreamCheckViewingMode } from '../services/configService';

export const streamCheckViewingKey = ['stream-check-viewing'];

const isMode = (value: string): value is StreamCheckViewingMode =>
  value === 'pause' || value === 'slow' || value === 'continuous';

export default function StreamCheckViewingField() {
  const tUi = useUiText();
  const client = useQueryClient();
  const config = useQuery({ queryKey: streamCheckViewingKey, queryFn: configService.getStreamCheckViewing });
  const save = useMutation({
    mutationFn: configService.updateStreamCheckViewing,
    onSuccess: value => client.setQueryData(streamCheckViewingKey, value),
  });
  if (config.isLoading) return <LinearProgress aria-label={tUi("Loading checks while watching")} />;
  if (config.isError || !config.data) return <Alert severity="error" action={<Button onClick={() => void config.refetch()}>{tUi("Retry")}</Button>}>{tUi("Could not load checks while watching.")}</Alert>;
  const dedicated = config.data.dedicated_checker;
  return <Stack spacing={0.5}>
    <TextField select SelectProps={{ native: true }} label={tUi("While watching")} id="stream-check-viewing"
      size="small" value={dedicated ? config.data.mode : 'pause'} disabled={save.isPending} sx={{ maxWidth: 450 }}
      helperText={dedicated
        ? tUi("Slow: one stream every 30 s while a channel plays, at full speed otherwise.")
        : tUi("Checking while watching needs a dedicated checking engine.")}
      onChange={event => {
        const mode = event.target.value;
        if (isMode(mode)) save.mutate({ mode });
      }}>
      <option value="pause">{tUi("Pause checks (metered connection)")}</option>
      <option value="slow" disabled={!dedicated}>{tUi("Keep checking slowly")}</option>
      <option value="continuous" disabled={!dedicated}>{tUi("Check continuously, slowly while watching")}</option>
    </TextField>
    {save.isError && <Alert severity="error">{tUi("Could not save checks while watching. Try again.")}</Alert>}
  </Stack>;
}
