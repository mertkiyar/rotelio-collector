() => {
  // Read loaded, visible one-way fare cards only.
  // Exclude price calendars, adjacent dates and promotional amounts.
  const path = location.pathname.match(/^\/(?:en\/)?ucuslar\/(\d+_\d+)-(\d+_\d+)\/(\d{8})\/(\d+)a\/economy\/all\/?$/);
  if (!path) throw new Error('A dated one-way flight results page is required.');
  const departure = document.getElementById('departure-input');
  if (!departure || departure.disabled || !departure.value) throw new Error('Search results have not loaded.');
  const text = (el, selector) => el.querySelector(selector)?.textContent.replace(/\s+/g, ' ').trim() || null;
  const cards = Array.from(document.querySelectorAll('#outbound-journeys > [id^="journey-"]'))
    .filter(el => el.getClientRects().length > 0 && /^journey-[A-Z]{3}-[A-Z]{3}-\d{8}-/.test(el.id));
  if (!cards.length) throw new Error('No loaded fare cards; an empty result is not verified as no flights.');
  const inputs = Array.from(document.querySelectorAll('#search input')).filter(el => el.type === 'text');
  return {
    schema_version: '1.0', source: 'obilet', transport_mode: 'flight', capture_method: 'rendered_dom',
    source_url: location.href, collected_at: new Date().toISOString(),
    search: {
      origin_id: path[1], destination_id: path[2],
      requested_date: `${path[3].slice(0,4)}-${path[3].slice(4,6)}-${path[3].slice(6,8)}`,
      adult_count: Number(path[4]), price_scope: Number(path[4]) === 1 ? 'per_person' : 'unverified',
      displayed_date: departure.value,
      origin_display: inputs[0]?.value || null, destination_display: inputs[1]?.value || null,
      currency_display: document.getElementById('currency-modal-btn')?.textContent.trim() || null
    },
    rows: cards.map(el => {
      const transfer = el.querySelector('.flights .transfers');
      const id = el.id.replace('journey-', '');
      const cardDate = id.match(/^[A-Z]{3}-[A-Z]{3}-(\d{8})-/)?.[1] || null;
      return {
        source_offer_id: id,
        departure_date_from_card: cardDate ? `${cardDate.slice(0,4)}-${cardDate.slice(4,6)}-${cardDate.slice(6,8)}` : null,
        carrier: text(el, '.flights .airline .name'),
        origin_airport_code: text(el, '.flights .flight-origin .airport'),
        destination_airport_code: text(el, '.flights .flight-arrival .airport'),
        origin_terminal: el.querySelector('.flights .flight-origin')?.getAttribute('title') || null,
        destination_terminal: el.querySelector('.flights .flight-arrival')?.getAttribute('title') || null,
        departure_text: text(el, '.flights .departure'),
        arrival_text: text(el, '.flights .arrival'),
        duration_text: text(el, '.flights .duration'),
        direct: !!transfer?.classList.contains('direct'),
        transfer_text: transfer?.textContent.replace(/\s+/g, ' ').trim() || null,
        checked_baggage_display: text(el, '.flights .baggage.down .font-bold'),
        cabin_baggage_display: text(el, '.flights .baggage.up .font-bold'),
        price_integer: text(el, '.price .amount-integer'),
        price_decimal: text(el, '.price .amount-decimal'),
        price_currency: text(el, '.price .amount-sign'),
        available_marker: el.classList.contains('available'),
        visible_text: el.innerText.trim()
      };
    })
  };
}
