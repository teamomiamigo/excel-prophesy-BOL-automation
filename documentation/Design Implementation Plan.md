	 
claude chats:
claude --resume 9ec7dbc5-3d04-4ae0-9b72-807bb03c5d0e

---
### 1 What the System Does

Replaces the manual daily Excel process (Technique and BOL Numbers New June 26.xlsx).
Katie uses this dashboard each morning to reconcile freight billing:

1. Compare what SG360 shipped (from the Technique Manifest) against what ALG has invoiced us
2. Calcuate the expected cost (tariff Rate + fuel surcharge) and compare it to ALG's prices
3. Approve or flag each record for discrepancies or do create a BOL or to say that the job is done and conciliated so you can put it away. 
4. Export those approved records to prophesy if necessary

Detailed Design for each change
Two types of records, A and B.
core insight into this process: (needs to be given to claude cuz i changed it slightly)
a technique manifest is created when it gets ready to be shipped. Most manifests go through ALG to be shipped out, which is a 3rd party service and ALG creates an invoice after calculated the weight pallets and pcs themselves and then creating a price based on their information, in a similar way to how we create the price.
invoice can come in two flavors depending on whether the shipping warehouse, not Katie, has already created a BOL in prophesy
Type A - No BOL yet (needs SID export)
- invoice identifier: Job number matches Technique Manifest Number
- example: invoice has ___ which is the number that is present in the manifest number after TEC_T
- action: Katie, after making sure numbers are good, has to import this into Prophesy and then create a BOL, and then a couple minutes later, the BOL gets updated and pulled from VisualMail/Prophesy for those approved records, an email is created that has the truncated information, and then it is sent out

Type B - BOL already exists (approve only situation)
- invoice identifier: the job number on the invoice is a BOL number in VisualMail connected to the Manifest, that already has a load created in prophesy
- example: invoice has ___ which matches with the BOL in technique
- action: just approve, the BOL number auto-fills from query in the beginning
- locations: Wolf building builds this automatically because they have access to Prophesy before they ship stuff out, but other facilities don't.

Implementation - how to detect which type
when ALG invoice CSV gets uploaded:
1. parse idenitifier field of job number from CSV (which is Job Name)
2. check if the number matches the BOL field in the technique manifest, but usually if it ends with a 14 it's a job order number and needs a BOL, and if its a smaller number it doesn't need a BOL, according to my calculations so far 
	1. still have a situation where it checks against a possible BOL, and if its there and matches then you can populate and if not you can leave it empty so she knows it needs to be created.

Invoice matching Logic
Strat 1 - BOL number to job order number on invoice match
Strat 2 - Technique manifest to job order number on invoice match

Invoice v Maniest Comparison
Should have information of shipment from both sources (weights, pallets pcs, calculated cost), BOL number / Job Number from Invoice, Trip and manifest number, cost differentials, editable notes, and action buttons

What's needed to implement mail parsing system
1. azure app registration in SG tenant
	1. permissions needed: mail.read on logistics mailboxes for certain ones
	2. auth: client credentials flow
2. Azure information for this
3. invoice mailbox information 
4. the new route it would go to to get the csv and get that information

Calculated cost formula (verification needed)
current formula:
- base cost = cost_per_100lb x weight_lbs / 100
- base tarriff = max(base_cost, minimum_weight)
- diesel price = API key
- FSC_pct = fuel_surcharge_rates band -> fsc_amount / 100
- calculated cost = base tariff x (1 + fsc_pct)

needs verification:
- is tariff rate per pallet or per manifest total
- is weight in lbs or CWT (100lb)
- is the FSC applied to the base rate or to something else 
- is the minimum_freight per pallet or per manifest

Open questions right now:
1. get an account CSV sample that is truncated or whatever
2. confirm calculated cost formula against a known real invoice and the access program working at it
3. how big is the manifest window (i'm thinking it should be a week, and if there is no BOL on a manifest and it hasn't been approved it should stay in the unapproved, as should anything else that seems incomplete.
4. 

Testing strategy
1. load dashboard that pulls from the queries and any invoices and checks for that
2. tests that the approve record system functions and that gets logged properly
3. making sure that flagged records are saved for when the rest of the order gets reconciliated another day
4. export to prophesy function works and fills in all the relevant data points necessary
5. reverts an approved record with ease
6. editing notes and checking for auto saves

Live data tests:
1. connecting to the live SQL server
2. access to any seed rate tables
3. pulls any new recent manifests that need to be reviewed
4. uploads the ALG invoice and has a small report of that process to make sure nothing bad happened
5. verifies all the costs
6. SID CSV check that happens when it's created to make sure that it works

notes for design iteration 2:
#### dashboard:
being able to remove a flag would be nice
we have to integrate the feature for parsing emails, let's start with doing it from my email and then learning how the code works so that we can change the testing time and then we can do it from there
- look for edge case situations in which we wouldn't want this, or a better way of tanya sending those emails to us so that we can verify this information.
how does the technique query pull and from what time frames or filtering method? we need to look at the query as it is handled in the excel and how katie can figure out the new manifests that she has to do on a basis. How is it being pulled now and if I get that query from the report, would you be able to figure out the same way on how that query is connected to the excel and it's filtering method?
how are you doing calculated cost without having the recent fuel price?  wouldn't that give you a wrong answer? is that hard coded right now?
for the approving logic, there needs to a better way of organizing it all right based on the information that we have, i'm not too sure.
also for the design logic, I'm seeing that sometimes the job number is in similar format to the trip number, just without that beginning zero. that logic should be into matching these, and if the technique comes with a BOL number that's similar, that pooled_in_load number should be populated with its matching BOL to the technique.

i think that priority one would be finding a way to test this live, do you think asking katie for that information would be valid? and then we can pull from the technique and see if we can actually see anything?

changes after iteration 2 of design:
invoice matching situation
- Matching key = Job Name field in CSV → TEC_T_0XXXXXX trip suffix ✅
multi-invoice accumulation for same trip
- TEC_T_0110710 has two invoices: Z557769 ($3,981) + Z557770 ($5,753) = $9,734.16 total ✅
- The invoice number field shows "Z557769, Z557770" ✅
- Re-uploading a Z-file that's already matched is idempotent (won't double-count) ✅
still has uneven in the cost department, the calculated cost isn't being done properly i think right now because a lot of the invoice calcuations and our calcualtions are off. . 

questions:
can't the invoice information provide the pre-pallet zip data too and then we can calculate that from there?
i want no invoice data also before in the application and have no invoice data in it until i upload the csvs
there also needs to be a better way of showing. either way, the invoice MUST have a technique, is there any way that we can go out looking that up and running based on the invoice numbers and do the testing in that form?
r u sure that 146 is co mingle. and if you can't upload it like that would you be able to add it to a record but have it so that the technique information and stuff could be updated at a later notice?
also i'm seeing huge cost differentials and im thinking its because of the per pallet ZIP info missing, but let's just get that from the invoices.
also sort the manifests that get filled in with invoice information to be moved to the top. sometimes manifests are made and invoices aren't created till later, so that's fine.
still, how are you filtering for technique manifests?

design changes for iteration 3:
- in the log, sort the approved and the pending automatically, if anything don't even show the pending ones cuz you can just see them in the dashboard, only the fully approved and set out ones.

questions after iteration 3:
can we not track the trailer number, keep the notes section empty 
how can you calculate the cost without knowing the zip numbers for the orders, that makes no sense right. also are you using the EIA live data and all the proper calculations, cuz you should now.
let's add a job number section so that for the invoices that don't have a technique manifest matching that they can be seen and organized in that way, but in reality there shouldn't be a reality where an invoice doesn't have a technique manifest number, you should be having that manifest and we need to figre out why he have this issue. i also want you to show job number so that i can verify that the tecfhnique numbers and the invoice numbers are matched. another way of verification that you can do is by matching the pallets and pieces, because those should be the exact same every time.
also you are not calculating anything for weight difference, so that needs to change and show the weight difference. show pallets and pieces difference between invoice and technique too, idk why that isn't working. 
something is wrong with the calculation for sure. let's go and check through this, and make sure you have the right information along with the right way of calculating this.

open tasks that need to be complete
1. getting the BOL number from technique/prophesy after it's been created, or allowing for manual entry
2. pull timestamps in a toolbar to see everything that happened and when and where things are coming from 
3. fix the duplicate unapprove_bol function that sometimes adds a second button (i guess)
4. getting granted on visual mail for AWD-SQL-WH4 for pallet export and we need that information for that
5. follow up with marge about locations v destination id and find the difference

later on
- automated morning pulls
- auto-parsing ALG invoice emails
- authentication and whatnot

questions:
- for katie, how long back would it be for the technique manifests to pull the information? usually how long does it take from when a shipment happens to how long it comes in as an invoice to get verified?
	- later, you would have to make the days_pulled 1 day, but still have all the previous records stable and staying there. right now 
	- you have set the days pulled back to 10 because of no db usage
	- do you still need the format sent to acccounting to be in that truncated format with all the other details as well
- for verification, you gotta add the way of using pallets and pcs as a way to see if a manifest lines up with the invoice

![[Pasted image 20260625094845.png]]

next steps:
have katie save the invoices to the folder, and from the shared folder we can automate pulling the csvs from there and then run that
- set up script to parse any new csvs in that folder every hour or something and then
fix the manifest to invoice situation
document the design for automation of scripts
remove the check email button
make the log show only completed ones
redesign the approval system
- the button that says send to katie is kinda dumb, I feel like it just needs to be an approve button, and then you do the proper things during the pop up
	- it should tell you that these manifests have a bol and these don't, and so it should only let you export the BOL file first and then you can send the rest to katie in that truncated format
	- should make it simple and clean, so that after that it'll be max approval, and then the BOLs of ones who have made it through this approval stage get pulled in
		- that would be later, having a part of the script that would make it so that any approved manifests that don't have a BOL in, pull the technique/prophesy script that would be able to do that
make an easier system of organization. the functioning should work moreover that Katie can open this up, see what's going on and check what's going on with ease. 
- Even though I want it pulling back from the last 10 (going to change that to more becasue we still have uncoupled invoices and manifests) we need to have an easier way of going through
- I want comingle invoices to be organized seperately in a drop down menu to be reviewed, and not grouped with the rest of the invoices.
- also keep invoices without a manifest at the bottom
there should also be logic implemented where when the technique manifests get reloaded and a new manifest comes in that matches an invoice (which shouldn't really happen), then it will match. 
- when it comes to the logic of matching, how are we speficially doing that? do we have the last resort logical plan in state that matches it by pieces, because that could easily match with a manifest too.
we also need to add filtering on the dashboard page, so that she can look up a a trip manifest and trip number, or an invoice number, or a job number, or a BOL
can we possibly do a method in which when you over over the calculated cost then it tells you the math that goes into with the real numbers about how that cost is created
I need to add a button to pull from the folder too, it can be automated to pull but i tihnk doing it at the same time on the days seems uneccessary actually, so have a button that pulls for manifests and one that pulls for invoices, and then you keep the upload invoice button too but i don't think it will be worth. and we can delete the check email button
for the top features that gives that data, I want it to show a little more. tell me number of techniques that are pending, invoices that are pending, ones that have both that are ready for approval, and then you can a place that can also say like waiting for full approval or something like that.





discuss with a fellow developer for understanding of development methodology @ sg360
- how do i deploy the site using our resources
- for integrating my application data to AWS, how should i go about organizing that?
- what are security measures that you have to take into for developing that I should as well
- What is the process that I should follow when working on an application development project?
- how can I be a good developer with good practice when it comes to creating a new application here?
- what is the method of deployment that I should do 

