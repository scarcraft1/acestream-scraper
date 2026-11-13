import apiClient from './apiClient';
import type { components } from '../types/api-generated';

export type DevicePlaylist = components['schemas']['DevicePlaylistOut'];
export type DevicePlaylistCreate = components['schemas']['DevicePlaylistCreate'];
export type DevicePlaylistUpdate = components['schemas']['DevicePlaylistUpdate'];
export type ChannelGroup = components['schemas']['ChannelGroupOut'];

const root = '/v1/device-playlists';

/** Playlists per device: which channel groups and channels each one leaves out. */
export const devicePlaylistService = {
  list: async (): Promise<DevicePlaylist[]> => (await apiClient.get<DevicePlaylist[]>(root)).data,
  groups: async (): Promise<ChannelGroup[]> => (await apiClient.get<ChannelGroup[]>(`${root}/groups`)).data,
  create: async (body: DevicePlaylistCreate): Promise<DevicePlaylist> => (await apiClient.post<DevicePlaylist>(root, body)).data,
  update: async (id: number, body: DevicePlaylistUpdate): Promise<DevicePlaylist> =>
    (await apiClient.put<DevicePlaylist>(`${root}/${id}`, body)).data,
  remove: async (id: number): Promise<void> => { await apiClient.delete(`${root}/${id}`); },
};
