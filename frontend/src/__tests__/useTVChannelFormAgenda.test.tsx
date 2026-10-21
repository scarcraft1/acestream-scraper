import { act, renderHook } from '@testing-library/react';
import React from 'react';
import { useTVChannelForm } from '../hooks/useTVChannelForm';
import { TVChannel } from '../types/tvChannelTypes';

const channel = {
  id: 3,
  name: 'M+ LaLiga',
  is_active: true,
  is_favorite: false,
  epg_id: 'MLaLiga.es',
  agenda_epg_id: 'futbolenlatv.m-plus-laliga',
  channel_number: 7,
  created_at: '2026-10-14T00:00:00Z',
  updated_at: '2026-10-14T00:00:00Z',
} as unknown as TVChannel;

const change = (name: string, value: string) =>
  ({ target: { name, value, type: 'text', checked: false } }) as unknown as React.ChangeEvent<HTMLInputElement>;

describe('football agenda field', () => {
  it('loads the current agenda into the edit form', () => {
    const { result } = renderHook(() => useTVChannelForm());
    act(() => result.current.handleOpenEditDialog(channel));
    expect(result.current.formData.agenda_epg_id).toBe('futbolenlatv.m-plus-laliga');
  });

  it('sends null when the field is emptied, so the agenda is removed', () => {
    // An emptied field that is silently ignored cannot be undone from the
    // form: that is what happens to the channel number.
    const { result } = renderHook(() => useTVChannelForm());
    act(() => result.current.handleOpenEditDialog(channel));
    act(() => result.current.handleFormChange(change('agenda_epg_id', '  ')));
    let payload: ReturnType<typeof result.current.validateForm> = null;
    act(() => {
      payload = result.current.validateForm();
    });
    expect(payload).not.toBeNull();
    expect(payload).toHaveProperty('agenda_epg_id', null);
  });

  it('sends the trimmed id when it is set', () => {
    const { result } = renderHook(() => useTVChannelForm());
    act(() => result.current.handleOpenEditDialog(channel));
    act(() => result.current.handleFormChange(change('agenda_epg_id', ' futbolenlatv.dazn-laliga ')));
    let payload: ReturnType<typeof result.current.validateForm> = null;
    act(() => {
      payload = result.current.validateForm();
    });
    expect(payload).toHaveProperty('agenda_epg_id', 'futbolenlatv.dazn-laliga');
  });
});
