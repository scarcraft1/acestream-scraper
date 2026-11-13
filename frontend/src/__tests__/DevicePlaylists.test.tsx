import React from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { ThemeProvider } from '@mui/material/styles';
import DevicePlaylists from '../components/playlist/DevicePlaylists';
import { createAppTheme } from '../theme';
import { devicePlaylistService } from '../services/devicePlaylistService';
import * as tvHooks from '../hooks/useTVChannels';

jest.mock('../services/devicePlaylistService', () => ({
  devicePlaylistService: { list: jest.fn(), groups: jest.fn(), create: jest.fn(), update: jest.fn(), remove: jest.fn() },
}));
jest.mock('../hooks/useTVChannels');

const service = devicePlaylistService as jest.Mocked<typeof devicePlaylistService>;
const row = {
  id: 1, name: 'Example room', owner_id: null, disabled_groups: [] as number[], enabled_channels: [] as number[],
  disabled_channels: [] as number[], created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z',
  channels: 3, total: 4, tabs: 3,
};

const renderPanel = () => render(
  <ThemeProvider theme={createAppTheme('light')}>
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><DevicePlaylists /></QueryClientProvider>
  </ThemeProvider>,
);

describe('Device playlists', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    (tvHooks.useTVChannelCatalog as jest.Mock).mockReturnValue({ data: [{ id: 7, name: 'Example One' }] });
    service.list.mockResolvedValue([row]);
    service.groups.mockResolvedValue([
      { id: 1, name: 'Alpha', parent_id: null, position: 0, channels: 2 },
      { id: 2, name: 'Gamma', parent_id: null, position: 1, channels: 1 },
      { id: 3, name: 'Gamma Sub', parent_id: 2, position: 0, channels: 1 },
    ]);
  });

  it('disables a group and shows its subgroups off with it', async () => {
    service.update.mockResolvedValue({ ...row, disabled_groups: [2], channels: 2, tabs: 1 });
    renderPanel();
    expect(await screen.findByText('3 of 4 channels · 3 tabs')).toBeInTheDocument();
    const gamma = await screen.findByRole('checkbox', { name: 'Gamma (1)' });
    expect(gamma).toBeChecked();
    fireEvent.click(gamma);
    await waitFor(() => expect(service.update).toHaveBeenCalledWith(1, { disabled_groups: [2] }));
    expect(await screen.findByText('2 of 4 channels · 1 tabs')).toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: 'Gamma Sub — off because «Gamma» is' })).toBeDisabled();
  });

  it('adds a device by name', async () => {
    service.create.mockResolvedValue({ ...row, id: 2, name: 'Kitchen' });
    renderPanel();
    fireEvent.change(await screen.findByLabelText('New device'), { target: { value: ' Kitchen ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add' }));
    await waitFor(() => expect(service.create).toHaveBeenCalledWith({ name: 'Kitchen' }));
  });
});
