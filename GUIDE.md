# Putting Insolvency Radar online (no coding needed)

Part A puts the app on a public web address using Google Cloud (done in your browser).
Part B turns that into a Google Play Store app (optional, later).

## Part A. Website on Google Cloud Run
1. Go to https://console.cloud.google.com and sign in with a Google account.
2. Accept the terms and add a billing method when asked. Cloud Run has a free monthly allowance,
   and small apps like this usually cost very little. Set a budget alert under Billing.
3. Click the project name at the top, choose New Project, name it insolvency-radar, click Create.
4. Click the Cloud Shell button (the >_ icon at the top right). A terminal opens at the bottom.
5. In the Cloud Shell toolbar choose the three-dot menu, then Upload, and pick insolvency_radar.tar.gz.
6. Paste these two lines into the terminal, pressing Enter after each:
       tar xzf insolvency_radar.tar.gz
       cd insolvency_radar
7. Paste this single line (it is one command):
       gcloud run deploy insolvency-radar --source . --region australia-southeast1 --allow-unauthenticated --min-instances 1
8. Answer Y when asked to enable services or create a repository. It takes 3 to 6 minutes.
9. When it finishes it prints a Service URL. Open it: that is your live app.
   Data appears a few minutes after the first start, once the app has collected notices.

Notes
- --min-instances 1 keeps the app running so the daily collection keeps happening. It costs a little each month.
- Stored data is rebuilt from the sources whenever the app restarts, so nothing is lost for good.
- To update later: change the files, repeat steps 5 to 7.
- To stop all charges: Cloud Run, select the service, Delete.

## Part B. Google Play Store
1. Finish Part A and confirm the web address works on your phone.
2. Create a Play Console account at https://play.google.com/console (one-time US$25 fee, identity check).
3. Easiest route for a non-technical person: use a free tool such as PWABuilder (https://www.pwabuilder.com).
   Paste your Cloud Run address, click Start, then choose Android and download the package.
4. In Play Console choose Create app, fill in the name, category (Business), and upload the .aab file from step 3.
5. Add a privacy policy page. It must say the app lists publicly available company notices and
   contact details of appointed practitioners. Free generators exist; host the text on any web page.
6. Complete the content questionnaire and submit for review. Review usually takes a few days.
