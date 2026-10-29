/**
 * NOAA GSOD weather mirrored into the KB by the weather-sync Lambda.
 * Source: https://registry.opendata.aws/noaa-gsod/ (s3://noaa-gsod-pds/<year>/<station>.csv, public domain).
 */
export const NOAA_GSOD_BUCKET = 'noaa-gsod-pds';
/** First year mirrored; every later year in the source bucket follows, up to the latest (often partial). */
export const DEFAULT_WEATHER_FROM_YEAR = '2020';

/** Major airport stations (USAF + WBAN ids), US and international. */
export const WEATHER_STATIONS = [
  '72530094846', '74486094789', '72295023174', '72509014739', '72219013874', '72202012839', '72494023234', '72793024233',
  '72565003017', '72259003927', '72243012960', '72278023183', '72386023169', '72658014922', '72405013743', '72408013739',
  '72606014764', '72698024229', '72572024127', '70273026451', '91182022521', '03772099999', '07157099999', '16242099999',
  '08221099999', '06240099999', '47662099999', '54511099999', '94767099999', '71624099999', '83780099999', '68816099999',
  '43003099999', '48698099999', '40582099999', '41194099999', '62366099999', '01384099999', '02464099999', '06660099999',
  '11036099999', '12375099999', '47108099999', '45007099999', '93119099999', '87582099999',
];
