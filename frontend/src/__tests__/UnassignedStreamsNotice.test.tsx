import React from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import UnassignedStreamsNotice from '../components/overview/UnassignedStreamsNotice';
import { acestreamChannelService } from '../services/channelService';
import { TestMemoryRouter } from '../testUtils/router';

jest.mock('../services/channelService', () => ({ acestreamChannelService: { getUnassignedSummary: jest.fn() } }));
const summary = acestreamChannelService.getUnassignedSummary as jest.Mock;

const renderNotice = () => render(
  <TestMemoryRouter><QueryClientProvider client={new QueryClient()}><UnassignedStreamsNotice /></QueryClientProvider></TestMemoryRouter>,
);

describe('Unassigned streams notice', () => {
  it('says how many sources have no channel and links to them', async () => {
    summary.mockResolvedValue({ total: 12, recent: 2, recent_days: 7, examples: ['Example A', 'Example B'] });
    renderNotice();
    expect(await screen.findByText(/12 sources have no TV channel/)).toHaveTextContent('2 arrived in the last 7 days, the latest: Example A, Example B.');
    expect(screen.getByRole('link', { name: 'Review' })).toHaveAttribute('href', '/acestream-channels?assigned=false');
  });

  it('shows nothing when every source has a channel', async () => {
    summary.mockResolvedValue({ total: 0, recent: 0, recent_days: 7, examples: [] });
    const { container } = renderNotice();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(container).toBeEmptyDOMElement();
  });
});
